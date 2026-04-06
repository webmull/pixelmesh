// PixelMesh V2 — Client
// Replaces AprilTag display with Manchester-encoded blink emission.

// ------------------------------------------------------------------ //
// Blink encoding (mirrors blink_encoder.py)
// ------------------------------------------------------------------ //

const NUM_BITS  = 5;
const PHASE_MS  = 200;
const NUM_GUARD = 6;   // dark guard frames before Manchester data

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
let mode = "DETECTION";
let detected = false;          // true once controller has located this device
let detectedAt = 0;            // timestamp of detection (for glow fade)

let clockOffset = 0;

let currentEffect  = null;
let effectStartTime = 0;
let effectSpeed    = 0.3;
let effectSpatialFreq = 1.5;
let effectBpm      = 100;
let effectOriginU  = 0.5;
let effectOriginV  = 0.5;
let deviceOrder    = [];
let sweepDwell     = 0.18;

let ws              = null;
let reconnectDelay  = 500;
let connectWatchdog = null;
let heartbeatTimer  = null;
let wakeLock        = null;

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
    ws.send(JSON.stringify({ type: "sync_ping", client_time: Date.now() }));

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
    setStatus("reconnecting…");
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 5000);
  };

  ws.onerror = () => { try { ws.close(); } catch {} };
}

function handleMessage(msg) {
  if (msg.type === "assigned") {
    myBlinkId     = msg.blink_id;
    myU           = msg.u ?? 0;
    myV           = msg.v ?? 0;
    myBlinkPhases = encodeId(myBlinkId);
    blinkStartMs  = Date.now();
    setStatus(`ID ${myBlinkId} – blinking`);
    return;
  }

  if (msg.type === "sync_pong") {
    const now = Date.now();
    const rtt = now - msg.client_time;
    clockOffset = msg.server_time - (msg.client_time + rtt / 2);
    return;
  }

  if (msg.type === "update_position") {
    myU = msg.u ?? myU;
    myV = msg.v ?? myV;
    // Controller found us — trigger orange glow
    detected  = true;
    detectedAt = Date.now();
    setStatus(`ID ${myBlinkId} – located ✓`);
    return;
  }

  if (msg.type === "mode") {
    mode = msg.mode;
    applyModeVisual();
    return;
  }

  if (msg.type === "effect") {
    mode = "SHOWTIME";
    currentEffect  = msg.effect;
    effectStartTime = msg.start_time;
    effectSpeed    = msg.speed ?? 0.3;
    effectSpatialFreq = msg.spatial_freq ?? 1.5;
    effectBpm      = msg.bpm ?? 100;
    effectOriginU  = msg.origin_u ?? 0.5;
    effectOriginV  = msg.origin_v ?? 0.5;

    if (msg.device_order) {
      deviceOrder = msg.device_order;
      sweepDwell  = msg.dwell ?? 0.18;
    } else {
      deviceOrder = [];
    }

    applyModeVisual();
    return;
  }

  if (msg.type === "reset") {
    mode = "DETECTION";
    currentEffect = null;
    detected = false;
    applyModeVisual();
    setStatus(myBlinkId !== null ? `ID ${myBlinkId} – blinking` : "waiting…");
    return;
  }
}

function applyModeVisual() {
  if (mode === "SHOWTIME") {
    blinkScreen.style.display = "none";
    showtime.style.display    = "block";
    // Ensure black background while effect loads
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
  if (mode === "SHOWTIME" || myBlinkId === null || myBlinkPhases.length === 0) return;

  const totalMs  = myBlinkPhases.length * PHASE_MS;
  const elapsed  = Date.now() - blinkStartMs;
  const phaseIdx = Math.floor((elapsed % totalMs) / PHASE_MS);
  const phase    = myBlinkPhases[phaseIdx];

  if (detected) {
    // Pulse orange glow: fast brighten then slow fade
    const age   = (Date.now() - detectedAt) / 1000;  // seconds since detection
    const pulse = Math.exp(-age * 0.6);               // exponential decay
    const blink = phase === 1 ? 1.0 : 0.0;

    const r = Math.round(255 * (blink * (1 - pulse) + pulse));
    const g = Math.round(140 * blink * (1 - pulse) + 80  * pulse);
    const b = Math.round(0);

    blinkScreen.style.background = `rgb(${r},${g},${b})`;

    // Stop special treatment once fully faded (>10s)
    if (age > 10) detected = false;
  } else {
    blinkScreen.style.background = phase === 1 ? "#ffffff" : "#000000";
  }
}

// ------------------------------------------------------------------ //
// Effects shader
// ------------------------------------------------------------------ //

function shade(u, v, t) {
  if (currentEffect === "wave") {
    const phase = 2 * Math.PI * (u * effectSpatialFreq - t * effectSpeed);
    const i = 0.5 + 0.5 * Math.sin(phase);
    return [i * 255, i * 255, i * 255];
  }

  if (currentEffect === "gradient") {
    let i = (u - t * effectSpeed) % 1;
    if (i < 0) i += 1;
    return [i * 255, i * 255, i * 255];
  }

  if (currentEffect === "binary_wave") {
    const phase = 2 * Math.PI * (u * effectSpatialFreq - t * effectSpeed);
    const i = Math.sin(phase) > 0 ? 1 : 0;
    return [i * 255, i * 255, i * 255];
  }

  if (currentEffect === "pulse") {
    const beat = Math.sin(2 * Math.PI * (effectBpm / 60) * t);
    const i = Math.max(0, beat);
    return [i * 255, i * 255, i * 255];
  }

  if (currentEffect === "click_ripple") {
    const dx = u - effectOriginU;
    const dy = v - effectOriginV;
    const dist = Math.sqrt(dx * dx + dy * dy);

    const duration   = 4.0;
    const progress   = (t % duration) / duration;
    const waveFront  = progress * 1.4;
    const ringWidth  = 0.04;
    const decay      = 1.2;

    const delta = dist - waveFront;
    const ring  = Math.exp(-(delta * delta) / ringWidth);
    const echo  = 0.5 * Math.exp(-((dist - (waveFront - 0.18)) ** 2) / (ringWidth * 1.8));
    let i = (ring + echo) * Math.exp(-dist * decay);
    i = Math.max(0, Math.min(1, i));

    return [i * 40, i * 170, i * 255];
  }

  return [0, 0, 0];
}

// ------------------------------------------------------------------ //
// Render loop
// ------------------------------------------------------------------ //

function renderLoop() {
  updateBlink();

  if (mode === "SHOWTIME" && currentEffect) {
    const t = (serverNow() - effectStartTime) / 1000;

    if (currentEffect === "sweep_bar" && deviceOrder.length > 0) {
      const w = projCanvas.width  = window.innerWidth;
      const h = projCanvas.height = window.innerHeight;
      const eCtx = ctx;
      eCtx.clearRect(0, 0, w, h);

      const totalCycle  = sweepDwell * deviceOrder.length;
      const elapsed     = t % totalCycle;
      const activeIndex = Math.floor(elapsed / sweepDwell);
      const localT      = (elapsed % sweepDwell) / sweepDwell;
      const myIndex     = deviceOrder.indexOf(deviceId);

      projCanvas.style.display = "block";
      showtime.style.background = "#000";

      if (myIndex === activeIndex) {
        const stripeW = w * 0.36;
        const stripeX = localT * w;
        const grad = eCtx.createLinearGradient(
          stripeX - stripeW / 2, 0,
          stripeX + stripeW / 2, 0
        );
        grad.addColorStop(0,   "black");
        grad.addColorStop(0.4, "rgba(255,140,80,0.6)");
        grad.addColorStop(0.5, "rgba(255,210,170,1)");
        grad.addColorStop(0.6, "rgba(255,140,80,0.6)");
        grad.addColorStop(1,   "black");
        eCtx.fillStyle = grad;
        eCtx.fillRect(stripeX - stripeW / 2, 0, stripeW, h);
      }

    } else {
      projCanvas.style.display = "none";
      const [r, g, b] = shade(myU, myV, t);
      showtime.style.background = `rgb(${r},${g},${b})`;
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
