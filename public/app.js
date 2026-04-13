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

const blinkScreen = document.getElementById("blinkScreen");
const statusPill  = document.getElementById("statusPill");
const showtime    = document.getElementById("showtime");
const projCanvas  = document.getElementById("projectionCanvas");
const effectCanvas = document.getElementById("effectCanvas");
const ctx         = projCanvas.getContext("2d");

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

let myBlinkId    = null;
let myBlinkPhases = [];
let blinkStartMs  = 0;

let myU = 0;
let myV = 0;
let calibrated = false;  // true once server has a position for this device (for effect rendering)

// ---- Phone state machine ----
// IDLE        black          disconnected
// WAITING     yellow         connected, detection not started
// BLINKING    white/black    detection active, not yet found
// FOUND       orange         found during active detection (stays orange when detection ends)
// MISSED      red flash      detection ended, this device not found
// MISSED_DONE black          after red flash — stays black until next detection
// SHOWTIME    effect/red     effect playing
const PS = { IDLE:"IDLE", WAITING:"WAITING", BLINKING:"BLINKING",
             FOUND:"FOUND", MISSED:"MISSED", MISSED_DONE:"MISSED_DONE", SHOWTIME:"SHOWTIME" };
let phoneState  = PS.IDLE;
let missedStart = 0;

let clockOffset = 0;

let currentEffect  = null;
let effectStartTime = 0;
let effectSpeed    = 0.3;
let effectSpatialFreq = 1.5;
let effectBpm      = 100;
let effectOriginU  = 0.5;
let effectOriginV  = 0.5;
let effectAngle    = 0;      // degrees: 0=L→R, 90=T→B, 180=R→L, 270=B→T
let effectR        = 255;
let effectG        = 255;
let effectB        = 255;
let effectR2       = 255;
let effectG2       = 0;
let effectB2       = 0;
let effectSplit    = 0.5;
let effectOrbRadius = 0.25;

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
  syncSamples  = [];
  clockOffset  = 0;
  synced       = false;
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
    if ("wakeLock" in navigator) {
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
  phoneState    = PS.IDLE;
  currentEffect = null;
  calibrated    = false;
  myBlinkPhases = [];
  blinkScreen.style.background = "#000";
  showtime.style.background    = "#000";
  showtime.style.display       = "none";
  blinkScreen.style.display    = "flex";
}

function handleMessage(msg) {
  if (msg.type === "shutdown") {
    goBlack();
    return;
  }

  if (msg.type === "server_hello") {
    const stored = localStorage.getItem("pm_build_id");
    if (stored && stored !== msg.build_id) {
      localStorage.setItem("pm_build_id", msg.build_id);
      if (phoneState === PS.FOUND) {
        setTimeout(() => location.reload(), 800);
      } else {
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:fixed;inset:0;z-index:9999;background:#00ff44";
        document.body.appendChild(overlay);
        let on = true;
        const iv = setInterval(() => {
          on = !on;
          overlay.style.background = on ? "#00ff44" : "#000";
        }, 300);
        setTimeout(() => { clearInterval(iv); location.reload(); }, 5000);
      }
      return;
    }
    localStorage.setItem("pm_build_id", msg.build_id);
    return;
  }

  if (msg.type === "assigned") {
    myBlinkId     = msg.blink_id;
    myU           = msg.u ?? 0;
    myV           = msg.v ?? 0;
    calibrated    = msg.calibrated === true;
    myBlinkPhases = encodeId(myBlinkId);
    blinkStartMs  = Date.now();
    phoneState    = PS.WAITING;
    setStatus(`ID ${myBlinkId}`);
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
    phoneState = PS.FOUND;
    setStatus(`ID ${myBlinkId} – located ✓`);
    return;
  }

  if (msg.type === "detection_started") {
    // Start from phase 0 (guard) so the decoder sees guard → Manchester immediately.
    // Previously this skipped past the guard, meaning the guard only appeared after
    // 40 Manchester phases (~12s), making minimum decode time ~25s instead of ~13s.
    // Stagger by blink ID so devices don't all flash in sync.
    const stagger = myBlinkId !== null ? (myBlinkId % myBlinkPhases.length) * PHASE_MS : 0;
    blinkStartMs = Date.now() - stagger;
    phoneState  = PS.BLINKING;
    missedStart = 0;
    return;
  }

  if (msg.type === "detection_ended") {
    if (phoneState === PS.BLINKING) {
      phoneState  = PS.MISSED;
      missedStart = Date.now();
    }
    // FOUND stays FOUND
    return;
  }

  if (msg.type === "mode") {
    // Only used to switch to SHOWTIME visuals if effect message isn't coming
    // DETECTION mode: no state change needed — assigned already set WAITING
    if (msg.mode === "SHOWTIME") applyModeVisual();
    return;
  }

  if (msg.type === "effect") {
    phoneState     = PS.SHOWTIME;
    currentEffect  = msg.effect;
    effectStartTime = msg.start_time;
    effectSpeed    = msg.speed ?? 0.3;
    effectSpatialFreq = msg.spatial_freq ?? 1.5;
    effectBpm      = msg.bpm ?? 100;
    effectOriginU  = msg.origin_u ?? 0.5;
    effectOriginV  = msg.origin_v ?? 0.5;
    effectAngle    = msg.angle ?? 0;
    effectR        = msg.color_r  ?? 255;
    effectG        = msg.color_g  ?? 255;
    effectB        = msg.color_b  ?? 255;
    effectR2       = msg.color2_r ?? 255;
    effectG2       = msg.color2_g ?? 0;
    effectB2       = msg.color2_b ?? 0;
    effectSplit     = msg.split      ?? 0.5;
    effectOrbRadius = msg.orb_radius ?? 0.25;
    applyModeVisual();
    return;
  }

  if (msg.type === "reset") {
    phoneState    = PS.WAITING;
    currentEffect = null;
    calibrated    = false;
    applyModeVisual();
    setStatus(myBlinkId !== null ? `ID ${myBlinkId}` : "waiting…");
    return;
  }
}

function applyModeVisual() {
  if (phoneState === PS.SHOWTIME) {
    blinkScreen.style.display = "none";
    showtime.style.display    = "block";
    blinkScreen.style.background = "#000";
  } else {
    showtime.style.display    = "none";
    blinkScreen.style.display = "flex";
  }
}

function setStatus(text) {
  statusPill.textContent = text;
}

// ------------------------------------------------------------------ //
// Blink emission
// ------------------------------------------------------------------ //

function updateBlink() {
  if (phoneState === PS.SHOWTIME || myBlinkId === null || myBlinkPhases.length === 0) return;

  switch (phoneState) {
    case PS.IDLE:
    case PS.MISSED_DONE:
      blinkScreen.style.background = "#000";
      break;

    case PS.WAITING:
      blinkScreen.style.background = "#ffcc00";
      break;

    case PS.BLINKING: {
      const totalMs  = myBlinkPhases.length * PHASE_MS;
      const phaseIdx = Math.floor((Date.now() - blinkStartMs) % totalMs / PHASE_MS);
      blinkScreen.style.background = myBlinkPhases[phaseIdx] === 1 ? "#ffffff" : "#000000";
      break;
    }

    case PS.FOUND:
      blinkScreen.style.background = "rgb(255, 100, 0)";
      break;

    case PS.MISSED: {
      const age = (Date.now() - missedStart) / 1000;
      if (age < 1.2) {
        const on = Math.floor(age / 0.2) % 2 === 0 && Math.floor(age / 0.2) < 6;
        blinkScreen.style.background = on ? "rgb(200, 0, 0)" : "#000";
      } else {
        phoneState = PS.MISSED_DONE;
        blinkScreen.style.background = "#000";
      }
      break;
    }
  }
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

  if (currentEffect === "orb") {
    // Single orb drifting on a Lissajous path around the room
    const ox = 0.5 + 0.4 * Math.cos(t * effectSpeed);
    const oy = 0.5 + 0.35 * Math.sin(t * effectSpeed * 1.3);
    const dist = Math.sqrt((u - ox) ** 2 + (v - oy) ** 2);
    const i = Math.max(0, 1 - dist / effectOrbRadius) ** 2;
    return [i * effectR, i * effectG, i * effectB];
  }

  if (currentEffect === "particles") {
    // Multiple orbs with staggered phases — firefly swarm
    const n = Math.max(2, Math.round(effectSpatialFreq * 2));
    let brightness = 0;
    for (let p = 0; p < n; p++) {
      const phase = (p / n) * Math.PI * 2;
      const ox = 0.5 + 0.38 * Math.cos(t * effectSpeed + phase);
      const oy = 0.5 + 0.38 * Math.sin(t * effectSpeed * 0.7 + phase * 1.3);
      const dist = Math.sqrt((u - ox) ** 2 + (v - oy) ** 2);
      brightness = Math.max(brightness, Math.max(0, 1 - dist / effectOrbRadius) ** 2);
    }
    return [brightness * effectR, brightness * effectG, brightness * effectB];
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

  if (phoneState === PS.SHOWTIME && currentEffect) {
    if (!calibrated) {
      // Not calibrated — stay black, detection phase already flagged this device
      projCanvas.style.display = "none";
      showtime.style.background = "#000";
    } else {

    const t = (serverNow() - effectStartTime) / 1000;
    projCanvas.style.display = "none";
    const [r, g, b] = shade(myU, myV, t);
    showtime.style.background = `rgb(${r},${g},${b})`;

    } // end calibrated
  }

  requestAnimationFrame(renderLoop);
}

// ------------------------------------------------------------------ //
// Resize
// ------------------------------------------------------------------ //

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  projCanvas.width  = w;
  projCanvas.height = h;
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

blinkScreen.style.display = "flex";
showtime.style.display    = "none";

connect();
renderLoop();
requestWakeLock();
