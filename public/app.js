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
  // Structure: [6 dark guard] + Manchester("1" + id_bits + id_bits + "0")
  // Manchester: '1'→[1,0], '0'→[0,1]
  const bits = blinkId.toString(2).padStart(NUM_BITS, '0');
  const binaryStr = '1' + bits + bits + '0';   // 2 + NUM_BITS*2 = 12 bits

  const phases = new Array(NUM_GUARD).fill(0);   // dark guard
  for (const ch of binaryStr) {
    if (ch === '1') phases.push(1, 0);
    else            phases.push(0, 1);
  }
  return phases;  // length: 6 + 24 = 30
}

// ------------------------------------------------------------------ //
// DOM
// ------------------------------------------------------------------ //

const statusPill     = document.getElementById("statusPill");
const waitingId      = document.getElementById("waitingId");
const crowdMsg       = document.getElementById("crowdMsg");
const likeBtn        = document.getElementById("likeBtn");
const likeCount      = document.getElementById("likeCount");
const positionCanvas = document.getElementById("positionCanvas");
const _posCtx        = positionCanvas.getContext("2d");
const knownPositions = {};   // blink_id → {u, v}

const _THUMBS_PATH = "M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z";

function _spawnFlyLikes() {
  const rect = likeBtn.getBoundingClientRect();
  const cx   = rect.left + rect.width  / 2;
  const cy   = rect.top  + rect.height / 2;
  const SIZE = 20;
  const NS   = "http://www.w3.org/2000/svg";
  const wrap = document.createElement("div");
  wrap.className = "fly-like";
  wrap.style.left = (cx - SIZE / 2) + "px";
  wrap.style.top  = (cy - SIZE / 2) + "px";
  wrap.style.setProperty("--dx", ((Math.random() - 0.5) * 60) + "px");
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
}

likeBtn.addEventListener("pointerdown", (e) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "like_tap" }));
  }
  likeBtn.classList.remove("popped");
  void likeBtn.offsetWidth;
  likeBtn.classList.add("popped");
  _spawnFlyLikes(e.clientX, e.clientY);
});


// ------------------------------------------------------------------ //
// Crowd count + rotating messages
// ------------------------------------------------------------------ //

let _crowdCount = 0;
let _msgIndex   = 0;
let _msgDeck    = [];   // shuffled queue — exhausted before any message repeats
let _msgTimer   = null;

// Combined pool — functions require a crowd count, strings always show.
// When alone, only strings are picked; when others are present the full
// pool is used so facts and crowd-count messages interleave naturally.
const _msgs = [
  // crowd-count messages
  n => n === 1 ? `You're amongst 1 other beautiful person`           : `You're amongst ${n} other beautiful people`,
  n => n === 1 ? `1 other phone in the room`                         : `${n} phones in the room and counting`,
  n => n === 1 ? `1 stranger about to become one screen with you`    : `${n} strangers about to become one screen`,
  n => n === 1 ? `Joined by 1 other — the more the merrier`          : `Joined by ${n} others — the more the merrier`,
  n => n === 1 ? `1 other person hasn't closed this screen either`   : `${n} people haven't closed this screen either`,
  n => n === 1 ? `Just you and 1 other so far`                       : `${n} people and the show hasn't even started`,
  n => n === 1 ? `1 other pixel in the room`                         : `${n} pixels and counting`,
  n => n === 1 ? `You're not alone — 1 other is here`                : `You're one of ${n + 1} — make it count`,
  n => n === 1 ? `1 other person keeping their screen on`            : `${n} people all staring at a black screen — trust the process`,
  n => n === 1 ? `Almost a crowd`                                    : `${n} phones. 1 show. Let's go`,
  n => n === 1 ? `You and 1 other are part of something`             : `${n} strangers, one room, one moment`,
  n => n === 1 ? `1 other person turned their brightness up`         : `${n} people who actually read the instructions`,
  n => n === 1 ? `You and 1 other are early`                         : `${n} people here before the magic starts`,
  n => n === 1 ? `1 other phone, fully charged hopefully`            : `${n} phones. Please be charged`,
  n => n === 1 ? `You're basically the warm-up act`                  : `${n} people warming up the room`,
  n => n === 1 ? `Just 1 other — this is either intimate or awkward` : `${n} people who didn't sit at the back`,
  n => n === 1 ? `1 other person wondering what this is`             : `${n} people wondering what this is`,
  n => n === 1 ? `You and 1 other. The beginning of something`       : `${n} screens about to become one`,
  n => n === 1 ? `1 other person trusting the process`               : `${n} people trusting the process`,
  n => n === 1 ? `You're patient. So is 1 other person`              : `${n} patient people`,
  n => n === 1 ? `1 other person in the dark with you`               : `${n} people in the dark with you`,
  n => n === 1 ? `You and 1 other are already part of the show`      : `You and ${n} others are already part of the show`,
  n => n === 1 ? `The person next to you is also staring at a phone` : `Everyone around you is staring at their phone. For once, that's correct`,
  n => n === 1 ? `1 other person hasn't put their phone away`        : `${n} people who didn't put their phone away`,
  n => n === 1 ? `You're a pixel. So is 1 other person`              : `You're all pixels now`,
  n => n === 1 ? `1 other person is also being patient`              : `${n} people. 0 of them know what's about to happen`,
  n => n === 1 ? `Just you, 1 other, and a black screen`             : `${n} phones pointed at the ceiling and the show hasn't started`,
  n => n === 1 ? `1 other person is already doing better than most`  : `${n} people already doing better than the ones who closed this`,
  n => n === 1 ? `You and 1 other are the early ones`                : `${n} people in the room. Only you lot connected`,
  n => n === 1 ? `Hold tight. 1 other is doing the same`             : `Hold tight. ${n} others are doing the same`,
  // always-shown messages
  `You're the first one here`,
  `Others will join soon`,
  `Keep this screen open`,
  `You're early — that's a good thing`,
  `The room is filling up`,
  `A group of flamingos is called a flamboyance`,
  `Cleopatra lived closer in time to the Moon landing than to the pyramids being built`,
  `Otters hold hands while sleeping so they don't drift apart`,
  `A day on Venus is longer than a year on Venus`,
  `Oxford University is older than the Aztec Empire`,
  `Wombats produce cube-shaped poo`,
  `The blob of toothpaste on your brush is called a nurdle`,
  `Bananas are slightly radioactive`,
  `A group of owls is called a parliament`,
  `Honey never goes off — they found 3000-year-old honey in Egyptian tombs and it was fine`,
  `Crows can recognise human faces and hold grudges`,
  `There are more possible games of chess than atoms in the observable universe`,
  `Penguins propose to their mates with a pebble`,
  `The inventor of the Pringles can is buried in one`,
];

function _shuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function _eligibleIndices() {
  return _crowdCount >= 1
    ? _msgs.map((_, i) => i)
    : _msgs.reduce((a, v, i) => (typeof v === 'string' ? [...a, i] : a), []);
}

function _renderMsg(idx) {
  const item = _msgs[idx];
  return typeof item === 'function' ? item(_crowdCount) : item;
}

function _advanceMsgIndex() {
  const eligible = _eligibleIndices();
  // Drop any queued indices that are no longer eligible (e.g. crowd left)
  _msgDeck = _msgDeck.filter(i => eligible.includes(i));
  // Refill and shuffle when deck is exhausted, never repeat current
  if (_msgDeck.length === 0) {
    _msgDeck = _shuffle(eligible.filter(i => i !== _msgIndex));
  }
  _msgIndex = _msgDeck.shift();
}

function _rotateCrowdMsg() {
  _advanceMsgIndex();
  crowdMsg.textContent = _renderMsg(_msgIndex);
}

function _setCrowdCount(n) {
  _crowdCount = n;
  crowdMsg.textContent = _renderMsg(_msgIndex);
  if (!_msgTimer) _msgTimer = setInterval(_rotateCrowdMsg, 5000);
}

function _startMsgTimer() {
  if (_msgTimer) { crowdMsg.textContent = _renderMsg(_msgIndex); return; }
  // Start at a random string (fact) entry
  const stringIndices = _msgs.reduce((a, v, i) => (typeof v === 'string' ? [...a, i] : a), []);
  _msgIndex = stringIndices[Math.floor(Math.random() * stringIndices.length)];
  crowdMsg.textContent = _renderMsg(_msgIndex);
  _msgTimer = setInterval(_rotateCrowdMsg, 5000);
}

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
};

// Named view states → which card to show
const VIEW_CARD = {
  idle:      "blink",    // disconnected / black
  waiting:   "waiting",  // "Get ready" screen
  blinking:  "blink",    // detection active — flashing
  game_wait: "blink",    // game in progress, not my turn — black
  located:   "located",  // position confirmed
  missed:    "blink",    // detection ended, not found — red flash
  effects:   "effects",  // showtime effect playing
  game:      "game",     // game card (countdown / bug / winner)
};

let view = "idle";

// Explicit display type for each card when shown.
// We set this directly rather than relying on CSS cascade (removing inline
// style is unreliable on some mobile browsers when the card starts hidden).
const CARD_DISPLAY = {
  blink:   "block",
  waiting: "flex",
  located: "flex",
  effects: "block",
  game:    "flex",
};

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
    el.style.display = k === active ? CARD_DISPLAY[k] : "none";
  }
}

// ------------------------------------------------------------------ //
// Device state
// ------------------------------------------------------------------ //

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
let effectAngle       = 0;      // degrees: 0=L→R, 90=T→B, 180=R→L, 270=B→T
let effectR           = 255;
let effectG           = 255;
let effectB           = 255;
let effectR2          = 255;
let effectG2          = 0;
let effectB2          = 0;
let effectSplit       = 0.5;
let effectPath        = [];   // snake: ordered blink_ids (nearest-neighbour path)

// ---- Bug game ----
let gameShowAt      = 0;    // server-time ms when bug should appear for this phone
let gameSlotMs      = 5000;
let gameTapped      = false;
let gameTimer          = null;
let myReactionMs       = null;  // this phone's tap time, shown in winner overlay
let countdownTimer     = null;
let _gameRoundStartAt  = 0;     // server timestamp when the 20s round begins
const gameBugWrap    = document.getElementById("gameBugWrap");
const gameSlotBar    = document.getElementById("gameSlotBar");
const gameProgress   = document.getElementById("gameProgress");
const countdownText  = document.getElementById("countdownText");
const bugHappy       = document.getElementById("bugHappy");
const bugScared      = document.getElementById("bugScared");
const gamePrompt     = document.getElementById("gamePrompt");
const gameResult     = document.getElementById("gameResult");
const gameWinner       = document.getElementById("gameWinner");
const gameMyBanner     = document.getElementById("gameMyBanner");
const gameMyBannerLabel = document.getElementById("gameMyBannerLabel");
const gameMyBannerTime  = document.getElementById("gameMyBannerTime");
const gameWinnerPhone  = document.getElementById("gameWinnerPhone");
const gameWinnerTime   = document.getElementById("gameWinnerTime");

let ws              = null;
let reconnectDelay  = 500;
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

document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState === "visible") await requestWakeLock();
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

function connect() {
  if (ws) { try { ws.close(); } catch {} ws = null; }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  connectWatchdog = setTimeout(() => {
    if (ws && ws.readyState === WebSocket.CONNECTING) {
      try { ws.close(); } catch {}
    }
  }, 3000);

  ws.onopen = () => {
    clearTimeout(connectWatchdog);
    reconnectDelay = 500;

    ws.send(JSON.stringify({ type: "hello", device_id: deviceId }));

    requestWakeLock();
    startHeartbeat();
    setStatus("connected – waiting for assignment…");
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    handleMessage(msg);
  };

  ws.onclose = () => {
    clearTimeout(connectWatchdog);
    ws = null;
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    stopSync();
    goBlack();
    setStatus("reconnecting…");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 5000);
  };

  ws.onerror = () => { try { ws.close(); } catch {} };
}

function goBlack() {
  currentEffect = null;
  calibrated    = false;
  myBlinkPhases = [];
  _cleanupGame();
  gameProgress.style.display     = "none";
  CARDS.effects.style.background = "#000";
  setView("idle");
}

function handleMessage(msg) {
  if (msg.type === "shutdown") {
    goBlack();
    return;
  }

  if (msg.type === "server_hello") {
    const stored = localStorage.getItem("pm_build_id");
    localStorage.setItem("pm_build_id", msg.build_id);
    if (stored !== null && stored !== msg.build_id) {
      document.body.style.background = "#00e676";
      setTimeout(() => location.reload(), 200);
      return;
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
      _startPositionMapAnim();
      setView("located");
    } else {
      waitingId.textContent = `You're phone #${myBlinkId + 1}`;
      _startMsgTimer();
      requestWakeLock();
      setView("waiting");
    }
    setStatus(`ID ${myBlinkId + 1}`);
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
    _startPositionMapAnim();
    setView("located");
    setStatus(`ID ${myBlinkId + 1} – located ✓`);
    return;
  }

  if (msg.type === "phone_located") {
    knownPositions[msg.blink_id] = {u: msg.u, v: msg.v};
    if (view === "located" && !_posMapAnim) _startPositionMapAnim();
    return;
  }

  if (msg.type === "crowd_map") {
    for (const [bid, pos] of Object.entries(msg.positions)) {
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
    gameProgress.style.display = "none";
    currentEffect     = msg.effect;
    effectStartTime   = msg.start_time;
    effectSpeed       = msg.speed ?? 0.3;
    effectSpatialFreq = msg.spatial_freq ?? 1.5;
    effectBpm         = msg.bpm ?? 100;
    effectOriginU     = msg.origin_u ?? 0.5;
    effectOriginV     = msg.origin_v ?? 0.5;
    effectAngle       = msg.angle ?? 0;
    effectR           = msg.color_r  ?? 255;
    effectG           = msg.color_g  ?? 255;
    effectB           = msg.color_b  ?? 255;
    effectR2          = msg.color2_r ?? 255;
    effectG2          = msg.color2_g ?? 0;
    effectB2          = msg.color2_b ?? 0;
    effectSplit       = msg.split ?? 0.5;
    effectPath        = msg.path  ?? [];
    setView("effects");
    return;
  }

  if (msg.type === "like_count") {
    likeCount.textContent = msg.count === 1 ? "1 like" : `${msg.count} likes`;
    return;
  }

  if (msg.type === "crowd_count") {
    _setCrowdCount(msg.count - 1); // subtract self
    return;
  }

  if (msg.type === "reset") {
    currentEffect = null;
    calibrated    = false;
    _cleanupGame();
    gameProgress.style.display = "none";
    waitingId.textContent = myBlinkId !== null ? `You're phone #${myBlinkId + 1}` : "Connecting…";
    _startMsgTimer();
    requestWakeLock();
    setView("waiting");
    setStatus(myBlinkId !== null ? `ID ${myBlinkId + 1}` : "waiting…");
    return;
  }

  if (msg.type === "game_countdown") {
    myReactionMs      = null;
    gameTapped        = false;
    currentEffect     = null;
    _gameRoundStartAt = msg.start_at + 3000;  // server time when game actually begins
    _cleanupGame();
    gameProgress.style.display = "none";
    setView("game");
    _startCountdown(msg.start_at);
    return;
  }

  if (msg.type === "game_show") {
    gameShowAt   = msg.show_at;
    gameSlotMs   = msg.slot_ms;
    gameTapped   = false;
    myReactionMs = null;
    _cleanupGame();
    // Show the game card immediately (black background) so the waiting
    // card is hidden during the pre-bug random delay before the bug appears.
    bugHappy.style.display  = "none";
    bugScared.style.display = "none";
    gameResult.textContent  = "";
    setView("game");
    const delay = gameShowAt - serverNow();
    gameTimer = setTimeout(_showHappyBug, Math.max(0, delay));
    return;
  }

  if (msg.type === "game_progress") {
    gameProgress.textContent   = `${msg.tapped} / ${msg.total} tapped`;
    gameProgress.style.display = "block";
    return;
  }

  if (msg.type === "game_winner" || msg.type === "game_end") {
    _showWinner(msg);
    return;
  }
}

function setStatus(text) {
  statusPill.textContent = text;
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
// Bug game
// ------------------------------------------------------------------ //

function _startCountdown(startAt) {
  _stopCountdown();
  bugHappy.style.display  = "none";
  bugScared.style.display = "none";
  gameResult.textContent  = "";

  function _tick() {
    const elapsed    = serverNow() - startAt;
    const remaining  = Math.ceil((3000 - elapsed) / 1000);
    const label      = remaining > 0 ? String(remaining) : "GO!";
    if (countdownText.textContent !== label) {
      countdownText.classList.remove("pop");
      void countdownText.offsetWidth;
      countdownText.textContent = label;
      countdownText.classList.add("pop");
    }
    if (elapsed >= 3800) {
      _stopCountdown();
      setView("game_wait");
    }
  }

  _tick();
  countdownTimer = setInterval(_tick, 100);
}

function _stopCountdown() {
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  countdownText.classList.remove("pop");
  countdownText.textContent = "";
}

function _showHappyBug() {
  _stopCountdown();
  gameTapped = false;
  bugHappy.style.display  = "block";
  bugScared.style.display = "none";
  gamePrompt.textContent  = "TAP!";
  gamePrompt.classList.add("pulsing");
  gameResult.textContent  = "";
  // Full-card tap zone — better than a small image target on mobile
  CARDS.game.addEventListener("pointerdown", _onGameTap);
  // Game-time bar: set start position from server clock, then transition to 0
  const elapsed   = Math.max(0, serverNow() - _gameRoundStartAt);
  const remaining = Math.max(0, 20000 - elapsed);
  gameSlotBar.style.transition = "none";
  gameSlotBar.style.transform  = `scaleX(${remaining / 20000})`;
  void gameSlotBar.offsetWidth;   // force reflow before starting transition
  gameSlotBar.style.transition = `transform ${remaining}ms linear`;
  gameSlotBar.style.transform  = "scaleX(0)";
  gameTimer = setTimeout(_hideGame, gameSlotMs);
}

function _onGameTap(e) {
  if (gameTapped) return;
  gameTapped = true;
  e.preventDefault();
  CARDS.game.removeEventListener("pointerdown", _onGameTap);
  clearTimeout(gameTimer);
  gameTimer = null;

  myReactionMs = Math.round(serverNow() - gameShowAt);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "game_tap", reaction_ms: myReactionMs }));
  }

  gameSlotBar.style.transition = "none";
  gamePrompt.classList.remove("pulsing");
  gamePrompt.textContent  = "";
  bugHappy.style.display  = "none";
  bugScared.style.display = "block";
  gameResult.textContent  = `${myReactionMs} ms`;
  // Stay on game card until winner is announced
}

function _showWinner(msg) {
  _cleanupGame();
  gameProgress.style.display = "none";

  // ---- Personal outcome (top, prominent) ----
  const isDraw  = msg.draw === true;
  const drawIds = msg.blink_ids || [];
  const iWon    = !isDraw && msg.blink_id !== undefined && msg.blink_id === myBlinkId;
  const iDrew   = isDraw && drawIds.includes(myBlinkId);

  if (iWon) {
    gameMyBannerLabel.textContent = "YOU WIN";
    gameMyBannerLabel.style.color = "#ffd740";
    gameMyBannerLabel.style.textShadow = "0 0 32px rgba(255,200,0,0.5)";
    gameMyBannerTime.textContent  = myReactionMs !== null ? `${myReactionMs} ms` : "";
    gameMyBannerTime.style.color  = "rgba(255,210,80,0.65)";
  } else if (iDrew) {
    gameMyBannerLabel.textContent = "IT'S A DRAW";
    gameMyBannerLabel.style.color = "#ffd740";
    gameMyBannerLabel.style.textShadow = "0 0 32px rgba(255,200,0,0.3)";
    gameMyBannerTime.textContent  = myReactionMs !== null ? `${myReactionMs} ms` : "";
    gameMyBannerTime.style.color  = "rgba(255,210,80,0.65)";
  } else if (myReactionMs !== null) {
    gameMyBannerLabel.textContent = "NOT THIS TIME";
    gameMyBannerLabel.style.color = "rgba(255,255,255,0.75)";
    gameMyBannerLabel.style.textShadow = "none";
    gameMyBannerTime.textContent  = `Your time: ${myReactionMs} ms`;
    gameMyBannerTime.style.color  = "rgba(255,255,255,0.35)";
  } else {
    gameMyBannerLabel.textContent = "YOU MISSED IT";
    gameMyBannerLabel.style.color = "rgba(255,80,80,0.85)";
    gameMyBannerLabel.style.textShadow = "none";
    gameMyBannerTime.textContent  = "";
  }

  // ---- Winner details (bottom) ----
  if (isDraw) {
    gameWinnerPhone.textContent = `Draw — ${drawIds.map(b => `#${b + 1}`).join(" & ")}`;
    gameWinnerTime.textContent  = `${msg.reaction_ms} ms each`;
  } else if (msg.blink_id !== undefined) {
    gameWinnerPhone.textContent = `Phone #${msg.blink_id + 1}`;
    gameWinnerTime.textContent  = `${msg.reaction_ms} ms`;
  } else {
    gameWinnerPhone.textContent = "No taps recorded";
    gameWinnerTime.textContent  = "";
  }

  bugHappy.style.display   = "none";
  bugScared.style.display  = "none";
  gamePrompt.textContent   = "";
  gameResult.textContent   = "";
  gameWinner.style.display = "flex";
  gameWinner.classList.remove("show");
  void gameWinner.offsetWidth;
  gameWinner.classList.add("show");
  setView("game");
}

// Called when the bug slot timer expires (phone missed the bug).
// Swaps to the black blink card to wait for game_winner.
function _hideGame() {
  _cleanupGame();
  setView("game_wait");
}

// Resets all game internals without touching the active card.
// Use this when transitioning away from game for other reasons
// (effect fires, reset, goBlack) so the caller controls the card.
function _cleanupGame() {
  if (gameTimer) { clearTimeout(gameTimer); gameTimer = null; }
  _stopCountdown();
  CARDS.game.removeEventListener("pointerdown", _onGameTap);
  gameSlotBar.style.transition = "none";
  gameSlotBar.style.transform  = "scaleX(0)";
  gamePrompt.classList.remove("pulsing");
  gameWinner.style.display = "none";
  gameWinner.classList.remove("show");
}

// ------------------------------------------------------------------ //
// Blink emission
// ------------------------------------------------------------------ //

function updateBlink() {
  if (VIEW_CARD[view] !== "blink") return;

  const card = CARDS.blink;

  if (view === "blinking" && myBlinkPhases.length > 0) {
    const totalMs  = myBlinkPhases.length * PHASE_MS;
    const phaseIdx = Math.floor((Date.now() - blinkStartMs) % totalMs / PHASE_MS);
    card.style.background = myBlinkPhases[phaseIdx] === 1 ? "#ffffff" : "#000000";
    return;
  }

  if (view === "missed") {
    const age = (Date.now() - missedStart) / 1000;
    if (age < 1.2) {
      const on = Math.floor(age / 0.2) % 2 === 0 && Math.floor(age / 0.2) < 6;
      card.style.background = on ? "rgb(200,0,0)" : "#000";
    } else {
      setView("idle");
    }
    return;
  }

  // idle / game_wait / any other blink-card view
  card.style.background = "#000";
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

function shade(u, v, t) {
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

  if (currentEffect === "binary_wave") {
    const phase = 2 * Math.PI * (d * effectSpatialFreq - t * effectSpeed);
    const i = Math.sin(phase) > 0 ? 1 : 0;
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

  if (currentEffect === "colour_flood") {
    // Normalise the directed coordinate to 0–1 across the actual u,v range
    // so the split point works correctly at any angle.
    const a  = effectAngle * Math.PI / 180;
    const ca = Math.cos(a), sa = Math.sin(a);
    const raw = u * ca + v * sa;
    const corners = [0, ca, sa, ca + sa];
    const dMin = Math.min(...corners), dMax = Math.max(...corners);
    const dn = dMax > dMin ? (raw - dMin) / (dMax - dMin) : 0.5;
    const blend = Math.max(0, Math.min(1, (dn - effectSplit) / 0.08 + 0.5));
    const r = effectR + (effectR2 - effectR) * blend;
    const g = effectG + (effectG2 - effectG) * blend;
    const b = effectB + (effectB2 - effectB) * blend;
    return [r, g, b];
  }

  if (currentEffect === "snake") {
    const total = effectPath.length;
    if (total === 0) return [0, 0, 0];
    const myIdx = effectPath.indexOf(myBlinkId);
    if (myIdx === -1) return [0, 0, 0];
    const head = (t * effectSpeed * total) % total;
    // Circular distance from this phone to the head
    let dist = Math.abs(myIdx - head);
    if (dist > total / 2) dist = total - dist;
    const tail = effectSpatialFreq;  // reuse spatial_freq slot as tail length
    const i = Math.max(0, 1 - dist / tail);
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "ripple") {
    // Origin sits on the edge of the u,v space at the given angle
    const a = effectAngle * Math.PI / 180;
    const ou = 0.5 + 0.5 * Math.cos(a);
    const ov = 0.5 + 0.5 * Math.sin(a);
    const dist = Math.sqrt((u - ou) ** 2 + (v - ov) ** 2);
    const phase = 2 * Math.PI * (dist * effectSpatialFreq - t * effectSpeed);
    const i = 0.5 + 0.5 * Math.sin(phase);
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "aurora") {
    const sp = effectSpeed;
    // Horizontal curtain bands drifting across the room, rippled by v
    const phase = u * 3.0 + Math.sin(v * 2.5 + t * sp * 0.5) * 0.5 - t * sp * 0.4;
    // Brightness: power curve gives distinct bright ribbons against dark sky
    const curtain = Math.pow(0.5 + 0.5 * Math.sin(phase * Math.PI), 2.5);
    // Hue oscillates teal (150°) ↔ blue-purple (210°) as the bands evolve
    const hue = (150 + Math.sin(phase * 0.8 - t * sp * 0.15) * 60 + 360) % 360;
    const lum = 0.06 + curtain * 0.50;
    return hslToRgb(hue / 360, 1.0, lum);
  }

  return [0, 0, 0];
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

  if (view === "effects" && currentEffect) {
    if (!calibrated) {
      projCanvas.style.display       = "none";
      CARDS.effects.style.background = "#000";
    } else {
      const t = (serverNow() - effectStartTime) / 1000;

      projCanvas.style.display       = "none";
      const [r, g, b] = shade(myU, myV, t);
      CARDS.effects.style.background = `rgb(${r},${g},${b})`;
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
