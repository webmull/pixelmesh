// (c) Adam Davis - adamdavis.co.uk
// PixelMesh V2 — Client
// Replaces AprilTag display with Manchester-encoded blink emission.

// ------------------------------------------------------------------ //
// Blink encoding (mirrors blink_encoder.py)
// ------------------------------------------------------------------ //

const NUM_BITS  = 9;
const PHASE_MS  = 300;
const NUM_GUARD = 4;   // dark guard frames before Manchester data

function encodeId(blinkId) {
  // Structure: [NUM_GUARD dark phases] + Manchester("1" + id_bits + id_bits + "0")
  // Manchester: '1'→[1,0], '0'→[0,1]
  const bits = blinkId.toString(2).padStart(NUM_BITS, '0');
  const binaryStr = '1' + bits + bits + '0';   // 2 + NUM_BITS*2 = 20 bits

  const phases = new Array(NUM_GUARD).fill(0);   // dark guard
  for (const ch of binaryStr) {
    if (ch === '1') phases.push(1, 0);
    else            phases.push(0, 1);
  }
  return phases;  // length: NUM_GUARD + 40
}

// ------------------------------------------------------------------ //
// DOM
// ------------------------------------------------------------------ //

const waitingId      = document.getElementById("waitingId");
const statusBar      = document.getElementById("statusBar");
const likeBtn        = document.getElementById("likeBtn");
const likeCount      = document.getElementById("likeCount");
const positionCanvas = document.getElementById("positionCanvas");
const _posCtx        = positionCanvas.getContext("2d");
const locatedPhoneId = document.getElementById("locatedPhoneId");
const knownPositions = {};   // blink_id → {u, v}

const _THUMBS_PATH = "M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z";

function _spawnFlyLikes() {
  const rect = likeBtn.getBoundingClientRect();
  const cx   = rect.left + rect.width  / 2;
  const cy   = rect.top  + rect.height / 2;
  const NS   = "http://www.w3.org/2000/svg";

  // One thumb up the middle...
  const SIZE = 22;
  const wrap = document.createElement("div");
  wrap.className = "fly-like";
  wrap.style.left = (cx - SIZE / 2) + "px";
  wrap.style.top  = (cy - SIZE / 2) + "px";
  wrap.style.setProperty("--dx", ((Math.random() - 0.5) * 50) + "px");
  const svg  = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width",  SIZE);
  svg.setAttribute("height", SIZE);
  const path = document.createElementNS(NS, "path");
  path.setAttribute("fill", "#ffffff");
  path.setAttribute("d", _THUMBS_PATH);
  svg.appendChild(path);
  wrap.appendChild(svg);
  document.body.appendChild(wrap);
  wrap.addEventListener("animationend", () => wrap.remove());

  // ...escorted by a burst of pixels (the brand's square-dot motif)
  for (let i = 0; i < 5; i++) {
    const px = document.createElement("div");
    px.className = "fly-pixel";
    const s = 5 + Math.random() * 4;
    px.style.width = px.style.height = s + "px";
    px.style.left = (cx - s / 2 + (Math.random() - 0.5) * rect.width * 0.7) + "px";
    px.style.top  = (cy - s / 2 + (Math.random() - 0.5) * 16) + "px";
    px.style.setProperty("--dx", ((Math.random() - 0.5) * 110) + "px");
    px.style.setProperty("--dy", (-(70 + Math.random() * 90)) + "px");
    px.style.animationDelay = (Math.random() * 90) + "ms";
    document.body.appendChild(px);
    px.addEventListener("animationend", () => px.remove());
  }
}

likeBtn.addEventListener("pointerdown", (e) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "like_tap" }));
  }
  likeBtn.classList.remove("popped");
  void likeBtn.offsetWidth;
  likeBtn.classList.add("popped");
  // Screen-blink flash: white circle, dark thumb, back in 140ms
  likeBtn.classList.add("flash");
  setTimeout(() => likeBtn.classList.remove("flash"), 140);
  _spawnFlyLikes(e.clientX, e.clientY);
});


// ------------------------------------------------------------------ //
// Crowd count + rotating messages
// ------------------------------------------------------------------ //

const projCanvas   = document.getElementById("projectionCanvas");
const effectCanvas = document.getElementById("effectCanvas");
const ctx          = projCanvas.getContext("2d");

// ------------------------------------------------------------------ //
// Card system — single source of truth for the display layer
// ------------------------------------------------------------------ //

// One card is shown at a time. setView() swaps cards by toggling
// inline style.display. The CSS defines the correct display type
// for each card's shown state (flex / block).

const CARDS = {
  blink:   document.getElementById("card-blink"),
  waiting: document.getElementById("card-waiting"),
  located: document.getElementById("card-located"),
  effects: document.getElementById("card-effects"),
  game:    document.getElementById("card-game"),
  end:     document.getElementById("card-end"),
};

// Named view states → which card to show
const VIEW_CARD = {
  idle:      "blink",    // disconnected / black
  waiting:   "waiting",  // "Get ready" screen
  blinking:  "blink",    // detection active — flashing
  located:   "located",  // position confirmed
  missed:    "blink",    // detection ended, not found — red flash
  effects:   "effects",  // showtime effect playing
  game:      "game",     // avatar race card
  ended:     "end",      // show over, closing card
};

let view = "idle";

/* Declared here, not down with the socket state, because setView() reads it -
   and setView is defined 200 lines before that block. It only worked because
   the first call happens at the very bottom of the file; anything that called
   setView earlier would have hit a temporal dead zone. */
let everConnected = false;   // false until the first successful WS open this page-load

// Explicit display type for each card when shown.
// We set this directly rather than relying on CSS cascade (removing inline
// style is unreliable on some mobile browsers when the card starts hidden).
const CARD_DISPLAY = {
  blink:   "block",
  waiting: "flex",
  located: "flex",
  effects: "block",
  game:    "flex",
  end:     "flex",
};

/* Every access to a card goes through here. app.html is cached separately from
   app.js, so a phone can run new script against older markup and find a card
   missing - and a bare CARDS.x.style threw, which on the render loop meant
   throwing on every frame and on the disconnect path meant a phone that
   dropped could never recover. The stub absorbs the call: a missing card
   costs that card, never the show.

   Deliberately not a Proxy - this runs inside the 60fps render loop on phones
   from 2016. */
const _CARD_STUB = {
  style: {},
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  addEventListener(){}, removeEventListener(){},
};
function card(name) { return CARDS[name] || _CARD_STUB; }

let _posMapAnim = null;

function _startPositionMapAnim() {
  _stopPositionMapAnim();
  function frame() {
    try { _drawPositionMap(); } catch(e) { /* don't kill the loop */ }
    _posMapAnim = requestAnimationFrame(frame);
  }
  _posMapAnim = requestAnimationFrame(frame);
}

function _stopPositionMapAnim() {
  if (_posMapAnim) { cancelAnimationFrame(_posMapAnim); _posMapAnim = null; }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && view === "located") _startPositionMapAnim();
});

function setView(name) {
  if (name !== "located") _stopPositionMapAnim();
  view = name;
  const active = VIEW_CARD[name];
  for (const [k, el] of Object.entries(CARDS)) {
    // Skip a card the page does not have. app.html is cached separately from
    // app.js, so a phone can end up running new script against older markup -
    // and this loop throwing took the whole app down with it: setView is on
    // every path, so the page never connected and sat on "Connecting..."
    // forever. The build_id check that repairs a stale page lives inside the
    // server_hello handler, which never ran. Missing one card should cost that
    // card, not the show.
    if (!el) continue;
    el.style.display = k === active ? CARD_DISPLAY[k] : "none";
  }
  // Connection chrome only where it can't pollute the show. Before the
  // first-ever connect the idle card also carries it, so a fresh load
  // reads as "connecting" rather than a blank screen.
  statusBar.style.display =
    (name === "waiting" || name === "located" || !everConnected) ? "flex" : "none";
}

// ------------------------------------------------------------------ //
// Device state
// ------------------------------------------------------------------ //

// iOS Safari permits pinch zoom even with user-scalable=no; block its
// proprietary gesture events so the page can never end up stuck zoomed.
// (Double-tap zoom is already suppressed by touch-action: manipulation.)
// Pre-connect boot state: the idle card is pure black by design, but a
// fresh page-load should never look broken - show the status bar in its
// connecting state until the first socket opens.
waitingId.textContent = "Connecting…";
statusBar.classList.add("warn");
statusBar.style.display = "flex";

// Liveness watchdog, outside the WS event flow entirely. The 8s
// handover lives inside onclose, which never fires if the constructor
// hung, the assign never arrived, or iOS froze the page's timers in the
// background and resurrected it with a dead socket. Wall-clock checks
// catch all of those; the reload lands on the cloud endpoint, which
// serves whichever of show or holding page is right.
const bootTime = Date.now();
let lastVisibleTs = Date.now();

/* One way out. Four paths independently wanted to reload - the liveness
   watchdog (three of its own branches), the 8s handover deadline, the
   build-id check, and a throwing constructor - and none knew about the
   others. A flapping server could have several in flight at once, which is
   how a phone ends up reloading in a loop instead of recovering.

   Also latched: once a reload is committed the page is going away, so a
   second caller has nothing useful to add. The reason is kept for the console
   because "why did that phone reload" is otherwise unanswerable after a show. */
let _reloadCommitted = false;
function commitReload(reason) {
  if (_reloadCommitted) return;
  if (view === "ended") return;   // never take the closing card away
  _reloadCommitted = true;
  try { console.info("[pixelmesh] reloading:", reason); } catch (e) {}
  location.reload();
}
function _livenessCheck() {
  if (document.hidden) return;
  if (view !== "idle") return;      // any assigned/show state is alive
  const now = Date.now();
  // Grace after returning to foreground: frozen clocks otherwise read
  // as instantly over-limit and reload a socket that is mid-reconnect.
  if (now - lastVisibleTs < 5000) return;
  // A handshake in flight is never stuck: the 3s connect watchdog
  // closes hung CONNECTING sockets, so this state is always young.
  if (ws && ws.readyState === WebSocket.CONNECTING) return;
  if (!everConnected) {
    if (now - bootTime > 15000) commitReload("never connected in 15s");
  } else if (ws && ws.readyState === WebSocket.OPEN) {
    // open socket but never left idle: assign lost somewhere
    if (lastOpenTs && now - lastOpenTs > 10000) commitReload("open socket, no assign in 10s");
  } else if (disconnectedSince && now - disconnectedSince > 10000) {
    // the 8s reloadTimer should have fired; frozen timers backstop
    commitReload("outage past the 10s backstop");
  }
}
setInterval(_livenessCheck, 3000);


["gesturestart", "gesturechange", "gestureend"].forEach((t) =>
  document.addEventListener(t, (e) => e.preventDefault())
);
document.addEventListener("dblclick", (e) => e.preventDefault());

let deviceId = localStorage.getItem("device_id");
if (!deviceId) {
  deviceId = (crypto.randomUUID
    ? crypto.randomUUID()
    : "dev_" + Math.random().toString(16).slice(2) + "_" + Date.now());
  localStorage.setItem("device_id", deviceId);
}

let myBlinkId     = null;
let myBlinkPhases = [];
let blinkStartMs  = 0;
/* How long this phone took to be found. Comes from the server, which owns the
   clock: an in-memory value here died on every reload, so refreshing the
   closing card lost the number while the phone id and the map - both
   server-sourced - survived. It also cannot be measured accurately from here.
   blinkStartMs looks like the right baseline and is not: it is the phase
   anchor for the blink cycle, backdated by a per-phone stagger and re-anchored
   whenever the page returns to the foreground. */
let foundMs = null;
let missedStart   = 0;

let myU = 0;
let myV = 0;
let calibrated = false;  // true once server has a position for this device

let clockOffset = 0;

let currentEffect     = null;
let effectStartTime   = 0;
let effectSpeed       = 0.3;
let effectSpatialFreq = 1.5;
let effectBpm         = 100;
let effectOriginU     = 0.5;
let effectOriginV     = 0.5;
let effectOriginExplicit = false;   // true → use origin_u/v as-is; false → derive from angle
let effectRipplePulse  = false;     // true → single half-arch travelling pulse (controller click-fire)
let effectWaveAngle    = 0;         // degrees in u,v space: bearing of the controller half-arch
let effectWaveAngleExplicit = false; // false → omnidirectional (no directional boost)
let effectAngle       = 0;      // degrees: 0=L→R, 90=T→B, 180=R→L, 270=B→T
let effectR           = 255;
let effectG           = 255;
let effectB           = 255;
let effectR2          = 255;
let effectG2          = 0;
let effectB2          = 0;
let effectSplit       = 0.5;
let effectGroups      = {};   // groups: blink_id → group_index
let effectColumnColors = [];  // groups: per-column [r, g, b] array

// ---- Avatar race ----
let raceActive        = false;
let raceTapHandlerOn  = false;
let raceInRoster      = false;    // true if my blink_id is in this round's runners
let raceRosterSize    = 0;
const raceMyAvatar    = document.getElementById("raceMyAvatar");
const raceMyName      = document.getElementById("raceMyName");
const raceMySub       = document.getElementById("raceMySub");
const raceRank        = document.getElementById("raceRank");
const racePrompt      = document.getElementById("racePrompt");
const raceBarMine     = document.getElementById("raceBarMine");
const raceBarLeader   = document.getElementById("raceBarLeader");
const raceWinner      = document.getElementById("raceWinner");
const raceWinnerWho   = document.getElementById("raceWinnerWho");
const raceWinnerSub   = document.getElementById("raceWinnerSub");

// Avatar generator — kept in sync with stage.js so the character on the
// projector matches the one painted into the phone's header.
function _raceHashF(n) {
  n = ((n ^ 61) ^ (n >>> 16)) >>> 0;
  n = ((n + (n << 3)) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 4)) >>> 0;
  n = ((n * 0x27D4EB2D) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 15)) >>> 0;
  return (n & 0x7FFFFFFF) / 0x7FFFFFFF;
}
function _raceHsl(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h * 6) % 2 - 1));
  const m = l - c / 2;
  const i = Math.floor(h * 6) % 6;
  let rgb;
  if      (i === 0) rgb = [c, x, 0];
  else if (i === 1) rgb = [x, c, 0];
  else if (i === 2) rgb = [0, c, x];
  else if (i === 3) rgb = [0, x, c];
  else if (i === 4) rgb = [x, 0, c];
  else              rgb = [c, 0, x];
  return `rgb(${Math.round((rgb[0]+m)*255)},${Math.round((rgb[1]+m)*255)},${Math.round((rgb[2]+m)*255)})`;
}
const RACE_SKIN_TONES = ["#f1c9a5", "#d9a07e", "#a87049", "#6d4524"];
const RACE_HAT_STYLES = ["beanie", "cap", "top", "none"];
function _raceAvatarFeatures(bid) {
  return {
    bodyHue:  _raceHashF(bid),
    hatHue:   _raceHashF(bid * 7 + 11),
    skin:     RACE_SKIN_TONES[Math.floor(_raceHashF(bid * 13 + 5) * RACE_SKIN_TONES.length)],
    hatStyle: RACE_HAT_STYLES[Math.floor(_raceHashF(bid * 23 + 3) * RACE_HAT_STYLES.length)],
  };
}
function _paintMyAvatar(bid) {
  if (!raceMyAvatar || bid == null) return;
  const cx = raceMyAvatar.width  / 2;
  const cy = raceMyAvatar.height / 2;
  const size = Math.min(raceMyAvatar.width, raceMyAvatar.height) * 0.9;
  const c = raceMyAvatar.getContext("2d");
  c.clearRect(0, 0, raceMyAvatar.width, raceMyAvatar.height);
  const f = _raceAvatarFeatures(bid);
  const headR = size * 0.22;
  const bodyW = size * 0.45;
  const bodyH = size * 0.50;
  const headCy = cy - size * 0.28;
  c.fillStyle = _raceHsl(f.bodyHue, 0.75, 0.5);
  c.fillRect(cx - bodyW / 2, cy - bodyH / 2, bodyW, bodyH);
  c.fillStyle = f.skin;
  const armW = size * 0.08;
  c.fillRect(cx - bodyW / 2 - armW, cy - bodyH * 0.35, armW, bodyH * 0.5);
  c.fillRect(cx + bodyW / 2,        cy - bodyH * 0.35, armW, bodyH * 0.5);
  const legW = size * 0.12;
  const legH = size * 0.20;
  c.fillStyle = _raceHsl(f.bodyHue, 0.45, 0.25);
  c.fillRect(cx - bodyW * 0.35 - legW / 2, cy + bodyH / 2, legW, legH);
  c.fillRect(cx + bodyW * 0.35 - legW / 2, cy + bodyH / 2, legW, legH);
  c.fillStyle = f.skin;
  c.beginPath();
  c.arc(cx, headCy, headR, 0, Math.PI * 2);
  c.fill();
  if (f.hatStyle !== "none") {
    c.fillStyle = _raceHsl(f.hatHue, 0.85, 0.45);
    if (f.hatStyle === "beanie") {
      c.beginPath();
      c.arc(cx, headCy - headR * 0.2, headR * 1.05, Math.PI, 0);
      c.fill();
    } else if (f.hatStyle === "cap") {
      c.fillRect(cx - headR, headCy - headR * 0.5, headR * 2, headR * 0.45);
      c.fillRect(cx - headR * 0.2, headCy - headR * 0.2, headR * 1.6, headR * 0.18);
    } else if (f.hatStyle === "top") {
      c.fillRect(cx - headR * 0.7, headCy - headR * 1.6, headR * 1.4, headR * 1.1);
      c.fillRect(cx - headR * 1.1, headCy - headR * 0.5, headR * 2.2, headR * 0.18);
    }
  }
}
function _ordinalLabel(rank) {
  if (rank == null) return "—";
  const j = rank % 10, k = rank % 100;
  if (j === 1 && k !== 11) return rank + "st";
  if (j === 2 && k !== 12) return rank + "nd";
  if (j === 3 && k !== 13) return rank + "rd";
  return rank + "th";
}


let ws              = null;
/* Generation counter. Every socket carries the number it was created with, and
   each handler drops out if it is no longer the current one. Without this, a
   socket closed by a newer connect() still ran its onclose - scheduling another
   reconnect, blacking the card out, arming a reload - on behalf of a
   connection nobody was waiting for any more. That is what produced
   overlapping chains closing each other's sockets. */
let wsGen           = 0;
let reconnectTimer  = null;   // at most one pending reconnect, ever
let lastOpenTs      = 0;      // wall-clock of the most recent successful WS open
let reconnectDelay  = 500;
let disconnectedSince = 0;   // wall-clock start of the current outage, 0 while connected
let reloadTimer     = null;  // hard deadline for handing over to the holding page
let connectWatchdog = null;
let heartbeatTimer  = null;
let wakeLock        = null;

// ---- Pre-sync desync offset ----
// Before clock sync is active each phone adds a large random time offset so
// effects look deliberately chaotic.  When sync kicks in the offset is cleared
// and everything snaps to server time simultaneously — the "wow moment".
// Seeded from deviceId so the offset is stable across reconnects.
function _deviceSeed(id) {
  let h = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = (h * 0x01000193) >>> 0;
  }
  return h;
}
const DESYNC_RANGE_MS = 8000;   // up to 8s of random pre-sync offset
let preSyncOffset = (_deviceSeed(deviceId) / 0xffffffff) * DESYNC_RANGE_MS;
let synced = false;   // true once we have at least one good clock sample

// ---- Adaptive clock sync ----
const SYNC_INTERVAL_MS  = 5000;   // ping every 5s while sync is active
const SYNC_BUFFER_SIZE  = 8;      // keep last N samples
const SYNC_EMA_ALPHA    = 0.25;   // smoothing factor toward new best estimate
let syncTimer           = null;
let syncSamples         = [];     // [{rtt, offset}, ...]

function startSync() {
  if (syncTimer) return;
  _sendSyncPing();
  syncTimer = setInterval(_sendSyncPing, SYNC_INTERVAL_MS);
}

function stopSync() {
  if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
  syncSamples   = [];
  clockOffset   = 0;
  synced        = false;
  preSyncOffset = (_deviceSeed(deviceId) / 0xffffffff) * DESYNC_RANGE_MS;
}

function _sendSyncPing() {
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ type: "sync_ping", client_time: Date.now() }));
}

// ------------------------------------------------------------------ //
// Wake lock
// ------------------------------------------------------------------ //

async function requestWakeLock() {
  try {
    if ("wakeLock" in navigator && (!wakeLock || wakeLock.released)) {
      wakeLock = await navigator.wakeLock.request("screen");
    }
  } catch {}
}

/* One handler for coming back to the foreground. This was two separate
   listeners doing related work, which is an easy way to change half of a
   behaviour by accident. Order is preserved from the originals. */
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState !== "visible") return;
  lastVisibleTs = Date.now();
  // Restart the disconnect clock too: it may have aged while frozen, and the
  // reconnect deserves its full window in the foreground.
  if (disconnectedSince) disconnectedSince = Date.now();
  await requestWakeLock();
  // Resync after a brief background: re-open the socket if it died, and
  // restart the blink cycle anchor so we begin a clean guard-run from now
  // rather than continuing mid-cycle. No-op for pages the OS killed
  // outright — those reload from scratch and re-issue 'hello'.
  if (!ws || ws.readyState !== WebSocket.OPEN) connect();
  if (myBlinkPhases.length > 0) blinkStartMs = Date.now();
});

// ------------------------------------------------------------------ //
// Clock sync
// ------------------------------------------------------------------ //

function serverNow() {
  if (!synced) return Date.now() + clockOffset + preSyncOffset;
  return Date.now() + clockOffset;
}

// ------------------------------------------------------------------ //
// Heartbeat
// ------------------------------------------------------------------ //

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ping" }));
    }
  }, 15000);
}

// ------------------------------------------------------------------ //
// WebSocket
// ------------------------------------------------------------------ //

/* One pending reconnect at a time. Several paths want to retry - onclose, a
   throwing constructor, returning to the foreground - and each used to arm its
   own timer, so a phone that flapped a few times ended up with several
   independent chains all reconnecting on their own backoff. */
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, 5000);
}

function connect() {
  // Never tear down a healthy socket. A CONNECTING one is bounded by the 3s
  // watchdog and will resolve on its own; closing it here - which is what the
  // visibilitychange handler effectively did on every return to foreground -
  // is the other half of the overlapping-chain bug.
  if (ws && (ws.readyState === WebSocket.OPEN ||
             ws.readyState === WebSocket.CONNECTING)) return;
  if (ws) { try { ws.close(); } catch {} ws = null; }

  const gen = ++wsGen;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  // Constructor can throw synchronously in some in-app browsers; without
  // this the page would sit on Connecting forever with no retry loop.
  let sock;
  try {
    sock = new WebSocket(`${proto}://${location.host}/ws`);
  } catch (e) {
    ws = null;
    scheduleReconnect();
    return;
  }
  ws = sock;

  connectWatchdog = setTimeout(() => {
    if (sock.readyState === WebSocket.CONNECTING) {
      try { sock.close(); } catch {}
    }
  }, 3000);

  // Every handler drops out if a newer connect() has superseded this socket.
  const stale = () => gen !== wsGen;

  sock.onopen = () => {
    if (stale()) { try { sock.close(); } catch {} return; }
    clearTimeout(connectWatchdog);
    everConnected = true;
    lastOpenTs = Date.now();
    reconnectDelay = 500;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    disconnectedSince = 0;
    if (reloadTimer) { clearTimeout(reloadTimer); reloadTimer = null; }

    ws.send(JSON.stringify({ type: "hello", device_id: deviceId }));

    requestWakeLock();
    startHeartbeat();
  };

  sock.onmessage = (ev) => {
    if (stale()) return;
    // Contained on purpose. handleMessage is a long if-chain over a payload
    // this page does not control, and an unexpected shape used to throw out of
    // the socket handler - losing that message and anything it would have done
    // after the throw. One bad frame should not cost the show.
    let msg;
    try { msg = JSON.parse(ev.data); }
    catch (e) { return; }
    try { handleMessage(msg); }
    catch (e) { /* keep the socket alive */ }
  };

  sock.onclose = () => {
    if (stale()) return;          // a superseded socket speaks for nobody
    clearTimeout(connectWatchdog);
    ws = null;
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    stopSync();
    // Pre-show drops keep the waiting card up with an amber status bar
    // instead of cutting to black; a page that has never connected keeps
    // its Connecting state; every other view blacks out as before.
    //
    // "ended" is exempt for the same reason "waiting" is: the closing card
    // needs no socket. It is a static souvenir people are being asked to
    // screenshot, and phones drop constantly - screen lock, a walk to the
    // exit, conference wifi. Blacking it out on close wiped the card
    // moments after it appeared, which read as a flicker to black.
    if (view !== "waiting" && view !== "ended" && everConnected) {
      goBlack();
    } else {
      // Text change re-centres the bar and moves the dot; nudge a
      // repaint in the same frame so iOS retires the old layer.
      waitingId.textContent = everConnected ? "Reconnecting…" : "Connecting…";
      statusBar.classList.add("warn");
      statusBar.style.transform = "translateX(-50%) translateZ(0)";
    }
    // Never on the closing card. This deadline exists to hand a phone over to
    // the holding page once the show is genuinely down - but when the show has
    // ENDED there is nothing to hand over to, and a reload throws away the one
    // thing the audience is being asked to keep. It was also the flicker:
    // socket fails, 8s later the page reloads, the card paints and dies again.
    if (!disconnectedSince && view !== "ended") {
      disconnectedSince = Date.now();
      // Hard 8s deadline: reconnects handle short blips, but once the
      // show is genuinely down, hand over to the holding page fast -
      // its probe rejoins automatically and identity survives in
      // localStorage, so an early handover costs nothing. A timer
      // (not an onclose check) so backoff gaps can't stretch the wait.
      reloadTimer = setTimeout(() => {
        // CONNECTING is a live handshake (watchdog-bounded), not stuck;
        // the liveness interval backstops if it dies.
        if (ws && (ws.readyState === WebSocket.OPEN ||
                   ws.readyState === WebSocket.CONNECTING)) return;
        commitReload("8s handover deadline");
      }, 8000);
    }
    scheduleReconnect();
  };

  sock.onerror = () => { if (!stale()) { try { sock.close(); } catch {} } };
}

function goBlack() {
  currentEffect = null;
  calibrated    = false;
  myBlinkPhases = [];
  _cleanupGame();
  card("effects").style.background = "#000";
  setView("idle");
}

function handleMessage(msg) {
  if (msg.type === "show_end") {
    foundMs = (typeof msg.found_ms === "number") ? msg.found_ms : null;
    showEndCard(msg.total_connected || 0);
    return;
  }

  if (msg.type === "shutdown") {
    // Not once the closing card is up. The natural order is: end the show,
    // finish the talk, quit pixelmesh - and people are still holding that
    // card on the way out. Blacking it out because the operator closed an app
    // they cannot see would take the souvenir away mid-screenshot.
    if (view !== "ended") goBlack();
    return;
  }

  if (msg.type === "server_hello") {
    // Compare against the build THIS PAGE is actually running (embedded
    // in the script tag's cache-bust param), not localStorage history.
    // The old localStorage comparison reloaded freshly-loaded, already
    // current pages after every deploy: connect, flash, pointless
    // reload, double connect. Now only genuinely stale parked pages
    // reload; a fresh page always matches and stays put.
    const tag = document.querySelector('script[src*="app.js"]');
    const myBuild = tag && (tag.src.match(/[?&]v=([^&]+)/) || [])[1];
    if (myBuild && msg.build_id && myBuild !== msg.build_id) {
      document.body.style.background = "#00e676";
      setTimeout(() => commitReload("build id differs from this page"), 200);
    }
    return;
  }

  if (msg.type === "assigned") {
    myBlinkId     = msg.blink_id;
    myU           = msg.u ?? 0;
    myV           = msg.v ?? 0;
    calibrated    = msg.calibrated === true;
    myBlinkPhases = encodeId(myBlinkId);
    blinkStartMs  = Date.now();
    if (calibrated) {
      knownPositions[myBlinkId] = {u: myU, v: myV};
      locatedPhoneId.textContent = `Phone #${myBlinkId + 1}`;
      _startPositionMapAnim();
      setView("located");
    } else {
      waitingId.textContent = `Connected · Phone ID ${myBlinkId + 1}`;
      _statusBarOk(true);
      requestWakeLock();
      setView("waiting");
    }
    return;
  }

  if (msg.type === "sync_pong") {
    const now    = Date.now();
    const rtt    = now - msg.client_time;
    const offset = msg.server_time - (msg.client_time + rtt / 2);
    syncSamples.push({ rtt, offset });
    if (syncSamples.length > SYNC_BUFFER_SIZE) syncSamples.shift();
    // Best estimate = sample with lowest RTT (least network jitter)
    const best = syncSamples.reduce((a, b) => a.rtt < b.rtt ? a : b);
    clockOffset = clockOffset * (1 - SYNC_EMA_ALPHA) + best.offset * SYNC_EMA_ALPHA;
    synced = true;
    // Report stats back so the controller can display them
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type:    "sync_report",
        rtt:     Math.round(best.rtt),
        offset:  Math.round(clockOffset),
        samples: syncSamples.length,
      }));
    }
    return;
  }

  if (msg.type === "sync_start") { startSync(); return; }
  if (msg.type === "sync_stop")  { stopSync();  return; }

  if (msg.type === "update_position") {
    myU        = msg.u ?? myU;
    myV        = msg.v ?? myV;
    calibrated = true;
    if (myBlinkId !== null) knownPositions[myBlinkId] = {u: myU, v: myV};
    if (myBlinkId !== null) locatedPhoneId.textContent = `Phone #${myBlinkId + 1}`;
    _startPositionMapAnim();
    setView("located");
    return;
  }

  if (msg.type === "phone_located") {
    // Legacy single-phone variant — kept for backwards compat with older
    // server versions; current server batches via "phones_located".
    knownPositions[msg.blink_id] = {u: msg.u, v: msg.v};
    if (view === "located" && !_posMapAnim) _startPositionMapAnim();
    return;
  }

  if (msg.type === "phones_located") {
    for (const [bid, pos] of Object.entries(msg.positions || {})) {
      knownPositions[parseInt(bid)] = {u: pos.u, v: pos.v};
    }
    if (view === "located" && !_posMapAnim) _startPositionMapAnim();
    return;
  }

  if (msg.type === "crowd_map") {
    for (const [bid, pos] of Object.entries(msg.positions || {})) {
      knownPositions[parseInt(bid)] = {u: pos.u, v: pos.v};
    }
    if (view === "located" && !_posMapAnim) _startPositionMapAnim();
    return;
  }

  if (msg.type === "detection_started") {
    // If we're already calibrated (position confirmed), stay located — the
    // server only sends detection_started to uncalibrated phones, but a
    // race on reconnect could deliver this message late.  Guard here so we
    // never regress from located to blinking.
    if (calibrated) return;
    // Clear stale positions from previous sessions — the server is starting
    // fresh detection so any dots on the map are no longer valid.
    for (const k in knownPositions) delete knownPositions[k];
    const stagger = myBlinkId !== null ? (myBlinkId % myBlinkPhases.length) * PHASE_MS : 0;
    blinkStartMs = Date.now() - stagger;
    missedStart  = 0;
    setView("blinking");
    return;
  }

  if (msg.type === "detection_ended") {
    if (view === "blinking") {
      missedStart = Date.now();
      setView("missed");
    }
    // located stays located
    return;
  }

  if (msg.type === "mode") {
    if (msg.mode === "SHOWTIME") setView("effects");
    return;
  }

  if (msg.type === "effect") {
    _cleanupGame();
    currentEffect     = msg.effect;
    effectStartTime   = msg.start_time;
    effectSpeed       = msg.speed ?? 0.3;
    effectSpatialFreq = msg.spatial_freq ?? 1.5;
    effectBpm         = msg.bpm ?? 100;
    effectOriginU     = msg.origin_u ?? 0.5;
    effectOriginV     = msg.origin_v ?? 0.5;
    effectOriginExplicit = msg.origin_explicit ?? false;
    effectRipplePulse  = msg.ripple_pulse ?? false;
    effectWaveAngle    = msg.wave_angle ?? 0;
    effectWaveAngleExplicit = msg.wave_angle != null;
    effectAngle       = msg.angle ?? 0;
    effectR           = msg.color_r  ?? 255;
    effectG           = msg.color_g  ?? 255;
    effectB           = msg.color_b  ?? 255;
    effectR2          = msg.color2_r ?? 255;
    effectG2          = msg.color2_g ?? 0;
    effectB2          = msg.color2_b ?? 0;
    effectSplit       = msg.split ?? 0.5;
    effectGroups      = msg.groups ?? {};
    effectColumnColors = msg.column_colors ?? [];
    setView("effects");
    return;
  }

  if (msg.type === "like_count") {
    likeCount.textContent = msg.count === 1 ? "1 like" : `${msg.count} likes`;
    likeCount.classList.remove("count-pop");
    void likeCount.offsetWidth;
    likeCount.classList.add("count-pop");
    return;
  }

  if (msg.type === "effect_stop") {
    currentEffect = null;
    return;
  }

  if (msg.type === "reset") {
    currentEffect = null;
    calibrated    = false;
    _cleanupGame();
    waitingId.textContent = myBlinkId !== null ? `Connected · Phone ID ${myBlinkId + 1}` : "Connecting…";
    statusBar.classList.toggle("warn", myBlinkId === null);
    requestWakeLock();
    setView("waiting");
    return;
  }

  if (msg.type === "race_start") {
    currentEffect = null;
    raceActive    = true;
    raceInRoster  = (msg.blink_ids || []).includes(myBlinkId);
    raceRosterSize = (msg.blink_ids || []).length;

    card("game").classList.add("mode-race");
    _resetRaceView();
    setView("game");
    return;
  }

  if (msg.type === "race_progress") {
    if (!raceActive) return;
    const positions = msg.positions || {};
    const mine = positions[String(myBlinkId)] ?? 0;
    let leader = 0;
    let rank   = 1;
    for (const [bid, v] of Object.entries(positions)) {
      if (v > leader) leader = v;
      // Strict greater-than for rank so ties share the same place.
      if (parseInt(bid, 10) !== myBlinkId && v > mine) rank++;
    }
    if (raceBarMine)   raceBarMine.style.width   = (mine   * 100).toFixed(1) + "%";
    if (raceBarLeader) raceBarLeader.style.width = (leader * 100).toFixed(1) + "%";
    if (raceRank && raceInRoster) {
      raceRank.textContent = `${_ordinalLabel(rank)} of ${raceRosterSize}`;
    }
    return;
  }

  if (msg.type === "race_end") {
    _showRaceWinner(msg);
    return;
  }

}

function _statusBarOk(flash) {
  statusBar.classList.remove("warn");
  if (flash) {
    statusBar.classList.remove("flash");
    void statusBar.offsetWidth;   // restart the animation
    statusBar.classList.add("flash");
    // Drop the class once the join blink finishes so the dot returns
    // to its idle wink animation instead of freezing on the last frame.
    setTimeout(() => statusBar.classList.remove("flash"), 1200);
  }
}

// ------------------------------------------------------------------ //
// Position map
// ------------------------------------------------------------------ //

function _drawPositionMap() {
  const size = Math.round(Math.min(window.innerWidth, window.innerHeight) * 0.78);
  const ctx = _posCtx;
  if (positionCanvas.width !== size || positionCanvas.height !== size) {
    positionCanvas.width  = size;
    positionCanvas.height = size;
  } else {
    ctx.clearRect(0, 0, size, size);
  }

  // Grid lines
  ctx.strokeStyle = "rgba(255,255,255,0.07)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const p = (i / 4) * size;
    ctx.beginPath(); ctx.moveTo(p, 0); ctx.lineTo(p, size); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, p); ctx.lineTo(size, p); ctx.stroke();
  }

  // Border
  ctx.strokeStyle = "rgba(255,255,255,0.18)";
  ctx.strokeRect(0.5, 0.5, size - 1, size - 1);

  // Other phones
  for (const [bid, pos] of Object.entries(knownPositions)) {
    if (parseInt(bid) === myBlinkId) continue;
    ctx.beginPath();
    ctx.arc(pos.u * size, pos.v * size, 4, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.5)";
    ctx.fill();
  }

  // Own phone — larger, glowing green (animated pulse)
  if (myBlinkId !== null && knownPositions[myBlinkId]) {
    const x = knownPositions[myBlinkId].u * size;
    const y = knownPositions[myBlinkId].v * size;
    const pulse = 0.5 + 0.5 * Math.sin(Date.now() / 1000 * Math.PI * 2 * 2); // 2 Hz
    const glowR = 18 + pulse * 24;        // 18–42 px
    const dotR  = 6  + pulse * 4;         // 6–10 px
    const alpha = 0.2 + pulse * 0.55;     // 0.2–0.75
    const grd = ctx.createRadialGradient(x, y, 0, x, y, glowR);
    grd.addColorStop(0, `rgba(0,230,118,${alpha.toFixed(2)})`);
    grd.addColorStop(1, "rgba(0,230,118,0)");
    ctx.beginPath();
    ctx.arc(x, y, glowR, 0, Math.PI * 2);
    ctx.fillStyle = grd;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y, dotR, 0, Math.PI * 2);
    ctx.fillStyle = "#00e676";
    ctx.fill();
  }
}

// ------------------------------------------------------------------ //
// Closing card
// ------------------------------------------------------------------ //

/* Drawn once, not animated. The located map pulses because it is telling you
   something is happening; this one is a souvenir being held still to be
   photographed, and a pulsing dot screenshots at whatever brightness it
   happened to be at. */
function _drawEndMap() {
  const c = document.getElementById("endMap");
  if (!c) return;
  const x = c.getContext("2d"), W = c.width, H = c.height;
  x.clearRect(0, 0, W, H);

  x.strokeStyle = "rgba(255,255,255,0.07)";
  x.lineWidth = 2;
  for (let i = 1; i < 4; i++) {
    x.beginPath(); x.moveTo(i / 4 * W, 0); x.lineTo(i / 4 * W, H); x.stroke();
  }
  for (let j = 1; j < 3; j++) {
    x.beginPath(); x.moveTo(0, j / 3 * H); x.lineTo(W, j / 3 * H); x.stroke();
  }

  // Dot radius scales with how full the room was. Fixed-size dots merge into
  // a smear once a few hundred phones are on the map.
  const n = Object.keys(knownPositions).length || 1;
  const r = Math.max(3, Math.min(7, 620 / Math.sqrt(Math.max(n, 25)) / 6));

  for (const [bid, pos] of Object.entries(knownPositions)) {
    if (parseInt(bid) === myBlinkId) continue;
    x.beginPath(); x.arc(pos.u * W, pos.v * H, r, 0, Math.PI * 2);
    x.fillStyle = "rgba(255,255,255,0.42)"; x.fill();
  }

  // No dot and no chip for a phone that was never located: better an honest
  // map of the room than a marker invented for someone who was not on it.
  const me = myBlinkId !== null ? knownPositions[myBlinkId] : null;
  if (!me) return;

  const mx = me.u * W, my = me.v * H, R = 80;
  const g = x.createRadialGradient(mx, my, 0, mx, my, R);
  g.addColorStop(0, "rgba(0,230,118,0.62)");
  g.addColorStop(1, "rgba(0,230,118,0)");
  x.beginPath(); x.arc(mx, my, R, 0, Math.PI * 2); x.fillStyle = g; x.fill();
  x.beginPath(); x.arc(mx, my, R * 0.42, 0, Math.PI * 2);
  x.lineWidth = 2.5; x.strokeStyle = "rgba(0,230,118,0.85)"; x.stroke();
  x.beginPath(); x.arc(mx, my, R * 0.17, 0, Math.PI * 2);
  x.fillStyle = "#00e676"; x.fill();

  // A chip, not bare text. Green type sitting straight on a field of white
  // dots is unreadable at phone size, and unreadable again once the shared
  // screenshot is viewed small.
  const fs = Math.max(24, W / 24);
  x.font = "700 " + fs + "px -apple-system,system-ui,sans-serif";
  const pw = x.measureText("YOU").width + 26, ph = fs + 16;
  // Flip to whichever side has room, or an edge-of-room phone points off-map.
  const left = mx > W * 0.5;
  const cx = left ? mx - R - 10 - pw : mx + R + 10;
  x.beginPath();
  x.moveTo(left ? mx - R : mx + R, my);
  x.lineTo(left ? cx + pw : cx, my);
  x.lineWidth = 2; x.strokeStyle = "rgba(0,230,118,0.8)"; x.stroke();
  x.beginPath();
  if (x.roundRect) x.roundRect(cx, my - ph / 2, pw, ph, ph / 2);
  else x.rect(cx, my - ph / 2, pw, ph);
  x.fillStyle = "#00e676"; x.fill();
  x.fillStyle = "#02140a"; x.textAlign = "center"; x.textBaseline = "middle";
  x.fillText("YOU", cx + pw / 2, my + 1);
}

function showEndCard(total) {
  const phone = document.getElementById("endPhone");
  const found = document.getElementById("endFound");
  const room  = document.getElementById("endTotal");

  // Em dash where there is no number rather than a 0 or a blank: some phones
  // are never found, and that is the point of the talk, not a bug to hide.
  if (phone) phone.textContent = myBlinkId !== null ? String(myBlinkId + 1) : "\u2014";
  if (found) found.textContent = foundMs !== null ? (foundMs / 1000).toFixed(1) + "s" : "\u2014";
  if (room)  room.textContent  = total > 0 ? total.toLocaleString("en-GB") : "\u2014";

  currentEffect = null;          // nothing should still be painting behind it
  setView("ended");
  _drawEndMap();
}

// ------------------------------------------------------------------ //
// Avatar race
// ------------------------------------------------------------------ //

function _resetRaceView() {
  if (raceMyName) raceMyName.textContent = raceInRoster
    ? `You are Phone #${(myBlinkId ?? 0) + 1}`
    : "Spectating";
  if (raceMySub) raceMySub.textContent = raceInRoster
    ? "Find me on the stage and tap to run"
    : "You aren't in this race";
  if (raceRank) raceRank.textContent = raceInRoster
    ? `1st of ${raceRosterSize || "?"}`
    : "—";
  if (raceBarMine)   raceBarMine.style.width   = "0%";
  if (raceBarLeader) raceBarLeader.style.width = "0%";
  if (raceWinner) raceWinner.classList.remove("show");
  if (racePrompt) racePrompt.textContent = "TAP";
  // Paint the user's own avatar so they can spot themselves in the
  // crowd of dots on the stage projection.
  if (raceInRoster) _paintMyAvatar(myBlinkId);
  else if (raceMyAvatar) {
    const c = raceMyAvatar.getContext("2d");
    c.clearRect(0, 0, raceMyAvatar.width, raceMyAvatar.height);
  }
  if (raceInRoster && !raceTapHandlerOn) {
    card("game").addEventListener("pointerdown", _onRaceTap);
    raceTapHandlerOn = true;
  } else if (!raceInRoster && raceTapHandlerOn) {
    card("game").removeEventListener("pointerdown", _onRaceTap);
    raceTapHandlerOn = false;
  }
}

function _onRaceTap(e) {
  if (!raceActive || !raceInRoster) return;
  e.preventDefault();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "game_tap" }));
  }
  card("game").classList.add("tapping");
  setTimeout(() => card("game").classList.remove("tapping"), 90);
}

function _showRaceWinner(msg) {
  raceActive = false;
  if (raceTapHandlerOn) {
    card("game").removeEventListener("pointerdown", _onRaceTap);
    raceTapHandlerOn = false;
  }
  // Manual stop with no winner — fall back to wave silently.
  if (msg.winner == null) return;
  const winnerBid = msg.winner;
  const wonByMe   = winnerBid === myBlinkId;
  if (raceWinner) {
    raceWinner.classList.toggle("you-won", wonByMe);
  }
  if (raceWinnerWho) raceWinnerWho.textContent = wonByMe
    ? "YOU WIN!"
    : `#${winnerBid + 1}`;
  if (raceWinnerSub) raceWinnerSub.textContent = wonByMe
    ? "First across the line"
    : `Phone ${winnerBid + 1} took it. Better luck next round`;
  if (raceWinner) raceWinner.classList.add("show");
  // Haptic punch on the winning phone so they feel the result, not just see it.
  if (wonByMe && navigator.vibrate) {
    try { navigator.vibrate([60, 40, 120, 40, 200]); } catch (e) {}
  }
}

// Tear down both games' state — called from reset, effect, and goBlack.
function _cleanupGame() {
  // Race teardown
  raceActive   = false;
  raceInRoster = false;
  if (raceTapHandlerOn) {
    card("game").removeEventListener("pointerdown", _onRaceTap);
    raceTapHandlerOn = false;
  }
  if (raceWinner)    raceWinner.classList.remove("show", "you-won");
  if (raceBarMine)   raceBarMine.style.width   = "0%";
  if (raceBarLeader) raceBarLeader.style.width = "0%";
  // Card-level state
  card("game").classList.remove("mode-race", "tapping");
}

// ------------------------------------------------------------------ //
// Blink emission
// ------------------------------------------------------------------ //

/* Date.now(), not serverNow(), and that is deliberate: the blink pattern is
   this phone's own identity being emitted for the camera, deliberately
   staggered so the room does not flash in unison. Effects are the opposite -
   they must land together, so they use server time. */
function updateBlink() {
  if (VIEW_CARD[view] !== "blink") return;

  const blinkCard = card("blink");

  if (view === "blinking" && myBlinkPhases.length > 0) {
    const totalMs  = myBlinkPhases.length * PHASE_MS;
    const phaseIdx = Math.floor((Date.now() - blinkStartMs) % totalMs / PHASE_MS);
    blinkCard.style.background = myBlinkPhases[phaseIdx] === 1 ? "#ffffff" : "#000000";
    return;
  }

  if (view === "missed") {
    const age = (Date.now() - missedStart) / 1000;
    if (age < 1.2) {
      const on = Math.floor(age / 0.2) % 2 === 0 && Math.floor(age / 0.2) < 6;
      blinkCard.style.background = on ? "rgb(200,0,0)" : "#000";
    } else {
      setView("idle");
    }
    return;
  }

  // idle / any other blink-card view
  blinkCard.style.background = "#000";
}

// ------------------------------------------------------------------ //
// Effects shader
// ------------------------------------------------------------------ //

function directedCoord(u, v) {
  const a = effectAngle * Math.PI / 180;
  // map 0–360° to a blended u/v coordinate; normalise to 0–1
  const raw = u * Math.cos(a) + v * Math.sin(a);
  // range of raw: [-1, 1] when angle=90, so remap to [0,1]
  return (raw + 1) / 2;
}

// Ring geometry. Must stay in step with RING_MAX_R / RING_ASPECT in
// effects.py, which draws the same annulus in the sidebar preview.
// 0.78 puts the widest point just past the room corner (0.707) so the ring
// starts off the crowd and sweeps in. Aspect 1.0 keeps it a circle in u/v
// space: an oval from above, but it reaches the side walls and the back row
// at the same moment, which is what reads from the stage.
const RING_MAX_R  = 0.78;
const RING_ASPECT = 1.0;

function shade(u, v, t) {
  // When clock sync is off, scramble each phone's effective position AND
  // time so effects look chaotic — and stay chaotic.  The 8s time offset
  // alone leaves spatial patterns aligned; jittering u/v fragments wave/
  // ripple/etc; per-phone time-scale + direction makes phones drift
  // apart continuously instead of all running the same animation late.
  // All jitter is hash-seeded from blinkId, so it's stable per phone and
  // snaps cleanly back to coordinated motion the moment sync engages.
  // Ripple is click-driven and depends on absolute u,v + origin distance;
  // the unsynced jitter below would scramble u/v and (worse) flip t for
  // half the phones, which makes the half-arch's delta <= 0 gate never
  // open and the falloff push past 1.0.  Skip jitter for ripple so the
  // wave still emanates spatially even without clock sync.
  if (!synced && myBlinkId !== null && currentEffect !== "ripple") {
    const jx = (_hashFloat(myBlinkId * 7  + 11) - 0.5) * 1.6;   // u +/- 0.8
    const jy = (_hashFloat(myBlinkId * 13 + 17) - 0.5) * 1.6;
    const jt = 0.4 + _hashFloat(myBlinkId * 23 +  5) * 1.4;     // 0.4x – 1.8x
    const dir = _hashFloat(myBlinkId * 31 + 41) > 0.5 ? 1 : -1; // half reverse
    u = u + jx;
    v = v + jy;
    t = t * jt * dir;
  }
  const d = directedCoord(u, v);
  const cr = effectR / 255, cg = effectG / 255, cb = effectB / 255;

  if (currentEffect === "wave") {
    const phase = 2 * Math.PI * (d * effectSpatialFreq - t * effectSpeed);
    const i = 0.5 + 0.5 * Math.sin(phase);
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "gradient") {
    let i = (d - t * effectSpeed) % 1;
    if (i < 0) i += 1;
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "pulse") {
    const beat = Math.sin(2 * Math.PI * (effectBpm / 60) * t);
    const i = Math.max(0, beat);
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "rainbow") {
    // Hue sweeps across the directed axis, cycling over time
    const hue = ((d * effectSpatialFreq - t * effectSpeed) % 1 + 1) % 1;
    return hslToRgb(hue, 1.0, 0.5);
  }

  if (currentEffect === "spotlight") {
    // Cursor-driven Gaussian centred at the controller's mouse position.
    // The operator drags across the camera preview and the phones inside
    // the radius brighten in real-time — phones outside fade to black.
    const ou = effectOriginExplicit ? effectOriginU : 0.5;
    const ov = effectOriginExplicit ? effectOriginV : 0.5;
    const r  = Math.max(0.05, effectSpatialFreq);
    const du = u - ou;
    const dv = v - ov;
    const sigma2 = r * r * 0.5;
    const i = Math.exp(-(du*du + dv*dv) / Math.max(sigma2, 1e-6));
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "ripple") {
    // Click-driven ripple: a single bright leading edge that flows
    // outward and decays exponentially behind it.  Each phone lights
    // up once as the wave-front arrives, then fades smoothly — no
    // trailing rings that would re-light it (which read as the wave
    // "bouncing").
    const ou = effectOriginExplicit ? effectOriginU : 0.5;
    const ov = effectOriginExplicit ? effectOriginV : 0.5;
    const dist = Math.sqrt((u - ou) ** 2 + (v - ov) ** 2);
    const front = t * effectSpeed;
    const totalWidth = 0.7;
    const delta = dist - front;

    let i = 0;
    if (delta <= 0 && delta >= -totalWidth) {
      const age = -delta / totalWidth;         // 0 at front → 1 at trailing edge
      i = Math.exp(-age * 3.0);                // bright front, monotonic fade
    }

    // Spatial falloff so far edges still register a faint shimmer.
    const falloff = Math.exp(-dist * dist * 0.6);
    i *= falloff;

    // Directional boost when the controller sent a wave_angle.
    if (effectWaveAngleExplicit && dist > 0.001) {
      const phoneAngle = Math.atan2(v - ov, u - ou);
      const waveAngleRad = effectWaveAngle * Math.PI / 180;
      const dirCos = Math.cos(phoneAngle - waveAngleRad);
      const dirMult = 0.25 + 0.75 * Math.pow(Math.max(0, dirCos), 0.7);
      i *= dirMult;
    } else {
      i *= 0.6;
    }

    // Fade out once the wave-front (plus its decay window) clears the
    // far corner of the room.  sqrt(2) ≈ 1.414 is the max distance.
    const liveTime = (1.414 + totalWidth) / Math.max(effectSpeed, 0.05);
    const fadeOut = t > liveTime
      ? Math.max(0, 1 - (t - liveTime) * 0.6)
      : 1;
    i *= fadeOut;

    const tint = Math.min(1, dist * 1.2);
    const colR = 100 - tint * (100 -  10);
    const colG = 180 - tint * (180 -  60);
    const colB = 255 - tint * (255 - 200);

    return [i * colR, i * colG, i * colB];
  }

  if (currentEffect === "sparkle") {
    // Each phone gets its own rate + phase.  Each flash is a soft tinkle
    // bloom (not a binary on/off), and the colour is picked per CYCLE
    // rather than per-phone — so even a single phone alternates between
    // colour A and colour B over time.  Switch happens at sin=0 so it's
    // invisible (brightness is 0 there).
    const h0 = _hashFloat(myBlinkId);
    const h1 = _hashFloat(myBlinkId * 7 + 1);
    const flashRate = 0.5 + h0;
    const threshold = 1.0 - 2.0 * effectSplit;
    const rawPhase  = t * effectSpeed * flashRate + h1;
    const sinVal    = Math.sin(2 * Math.PI * rawPhase);
    let i = 0;
    if (sinVal > threshold) {
      const norm = (sinVal - threshold) / Math.max(1.0 - threshold, 1e-6);
      i = norm * norm * (3.0 - 2.0 * norm);   // smoothstep — soft bell
    }
    const cycle = Math.floor(rawPhase);
    const h2 = _hashFloat(myBlinkId * 13 + 2 + cycle * 97);
    if (h2 < 0.5) return [i * effectR,  i * effectG,  i * effectB];
    else          return [i * effectR2, i * effectG2, i * effectB2];
  }

  if (currentEffect === "sections") {
    // Divide the crowd into a n_cols × n_rows grid; checkerboard A/B colours;
    // diagonal sweep wave animates the sections when speed > 0.
    const n_cols = Math.max(1, Math.round(effectSpatialFreq));
    const n_rows = Math.max(1, Math.round(effectBpm));   // bpm slot reused as rows
    const col = Math.min(n_cols - 1, Math.floor(myU * n_cols));
    const row = Math.min(n_rows - 1, Math.floor(myV * n_rows));
    const isA = (col + row) % 2 === 0;
    const colFrac = n_cols > 1 ? col / (n_cols - 1) : 0.5;
    const rowFrac = n_rows > 1 ? row / (n_rows - 1) : 0.5;
    const wave = Math.sin(2 * Math.PI * ((colFrac + rowFrac) * 0.5 - t * effectSpeed));
    const i = wave > 0 ? 1.0 : 0.0;
    if (isA) return [i * effectR,  i * effectG,  i * effectB];
    else     return [i * effectR2, i * effectG2, i * effectB2];
  }

  if (currentEffect === "groups") {
    // Server assigns each phone a group index (sorted by u so every column
    // has equal phone count) and sends column_colors[col] = [r,g,b] picked
    // by the operator.  Fallback to spatial split / palette default for
    // uncalibrated phones or missing payload.
    const n   = Math.max(2, Math.round(effectSpatialFreq));
    const col = (myBlinkId in effectGroups)
      ? effectGroups[myBlinkId]
      : Math.min(n - 1, Math.floor(myU * n));
    const c = effectColumnColors[col];
    let r, g, b;
    if (c) { r = c[0]; g = c[1]; b = c[2]; }
    else   { r = effectR; g = effectG; b = effectB; }
    // Chase: speed=0 leaves all columns lit; speed>0 sweeps a brightness
    // focus across them so only the "current" column glows fully.
    if (effectSpeed > 0.001) {
      const phase = (((col / n) - t * effectSpeed) % 1 + 1) % 1;
      const focus = 0.5 + 0.5 * Math.cos(2 * Math.PI * phase);
      r *= focus; g *= focus; b *= focus;
    }
    return [r, g, b];
  }

  if (currentEffect === "ring") {
    // Breathing ring: a soft annulus whose radius eases in from the crowd
    // edge to the centre and back out, one breath per 1/speed seconds.
    // cos() eases at both ends and loops with no seam at the wrap point.
    const du = u - 0.5;
    const dv = (v - 0.5) / RING_ASPECT;
    const dist = Math.sqrt(du * du + dv * dv);
    const p = ((t * effectSpeed) % 1 + 1) % 1;
    const radius = RING_MAX_R * (0.5 + 0.5 * Math.cos(2 * Math.PI * p));
    const w = Math.max(0.02, effectSpatialFreq);
    const d = dist - radius;
    const i = Math.exp(-(d * d) / (2 * w * w));
    return [i * effectR, i * effectG, i * effectB];
  }

  return [0, 0, 0];
}

// Wang integer hash → float [0, 1).  Used to give each phone its own
// pseudo-random rate/phase/color without any server-side per-phone data.
function _hashFloat(n) {
  n = ((n ^ 61) ^ (n >>> 16)) >>> 0;
  n = ((n + (n << 3)) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 4)) >>> 0;
  n = ((n * 0x27D4EB2D) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 15)) >>> 0;
  return (n & 0x7FFFFFFF) / 0x7FFFFFFF;
}

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h * 6) % 2 - 1));
  const m = l - c / 2;
  let r, g, b;
  const i = Math.floor(h * 6);
  if      (i === 0) { r = c; g = x; b = 0; }
  else if (i === 1) { r = x; g = c; b = 0; }
  else if (i === 2) { r = 0; g = c; b = x; }
  else if (i === 3) { r = 0; g = x; b = c; }
  else if (i === 4) { r = x; g = 0; b = c; }
  else              { r = c; g = 0; b = x; }
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

// ------------------------------------------------------------------ //
// Render loop
// ------------------------------------------------------------------ //

function renderLoop() {
  updateBlink();

  if (view === "effects") {
    projCanvas.style.display = "none";
    if (!currentEffect || !calibrated) {
      // No active effect (e.g. operator armed ripple → effect_stop) or
      // pre-calibration: hold black so the previous frame's colour
      // doesn't linger on screen.
      card("effects").style.background = "#000";
    } else {
      const t = (serverNow() - effectStartTime) / 1000;
      const [r, g, b] = shade(myU, myV, t);
      card("effects").style.background = `rgb(${r},${g},${b})`;
    }
  }

  requestAnimationFrame(renderLoop);
}

// ------------------------------------------------------------------ //
// Resize
// ------------------------------------------------------------------ //

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  projCanvas.width    = w;
  projCanvas.height   = h;
  effectCanvas.width  = w;
  effectCanvas.height = h;
  const vh = h * 0.01;
  document.documentElement.style.setProperty("--vh", `${vh}px`);
}

window.addEventListener("resize", resize);
window.addEventListener("orientationchange", resize);
resize();

// ------------------------------------------------------------------ //
// Boot
// ------------------------------------------------------------------ //

setView("idle");
connect();
renderLoop();
requestWakeLock();
