// PixelMesh V2 — Simulator
// Spawns N fake clients in the browser, each connecting via WebSocket
// and blinking their assigned blink_id.
// Useful for testing the server and the blink detection pipeline.

const NUM_BITS  = 8;
const PHASE_MS  = 450;
const NUM_GUARD = 6;

function encodeId(blinkId) {
  const bits = blinkId.toString(2).padStart(NUM_BITS, '0');
  const binaryStr = '1' + bits + bits + '0';
  const phases = new Array(NUM_GUARD).fill(0);
  for (const ch of binaryStr) {
    if (ch === '1') phases.push(1, 0);
    else            phases.push(0, 1);
  }
  return phases;  // length: 30
}

// ------------------------------------------------- //

const grid     = document.getElementById("grid");
const wsStatus = document.getElementById("wsStatus");

let clients   = [];
let animFrame = null;

function updateWsStatus() {
  if (clients.length === 0) {
    wsStatus.textContent = "idle";
    wsStatus.className   = "";
    return;
  }
  const open = clients.filter(c => c.ws && c.ws.readyState === WebSocket.OPEN).length;
  wsStatus.textContent = `${open} / ${clients.length} connected`;
  if (open === clients.length) {
    wsStatus.className = "connected";
  } else if (open > 0) {
    wsStatus.className = "partial";
  } else {
    wsStatus.className = "";
  }
}

class SimClient {
  constructor(idx) {
    this.idx        = idx;
    this.deviceId   = `sim-${idx}-${Math.random().toString(36).slice(2, 8)}`;
    this.blinkId    = null;
    this.phases     = [];
    this.startMs    = 0;
    this.mode       = "WAITING";
    this.u          = 0;
    this.v          = 0;
    this.clockOffset = 0;
    this.detected   = false;

    this.currentEffect  = null;
    this.effectStartTime = 0;
    this.effectSpeed    = 0.3;
    this.effectSpatialFreq = 1.5;
    this.effectBpm      = 100;
    this.effectOriginU  = 0.5;
    this.effectOriginV  = 0.5;
    this.deviceOrder    = [];
    this.sweepDwell     = 0.18;

    this.ws = null;

    // DOM
    this.cell = document.createElement("div");
    this.cell.className = "cell";
    this.cell.innerHTML = `<div class="dot"></div><div class="id">?</div><div class="label">${this.deviceId.slice(0,12)}</div>`;
    this.idEl = this.cell.querySelector(".id");
    grid.appendChild(this.cell);

    // Random position with collision avoidance
    const CELL = 120, PAD = 20;
    const gw   = grid.clientWidth  || window.innerWidth;
    const gh   = grid.clientHeight || window.innerHeight - 70;
    const min_dist = CELL + PAD;
    let x = 0, y = 0;
    for (let attempt = 0; attempt < 200; attempt++) {
      x = Math.random() * Math.max(0, gw - CELL);
      y = Math.random() * Math.max(0, gh - CELL);
      const ok = clients.every(c => {
        const dx = parseFloat(c.cell.style.left) - x;
        const dy = parseFloat(c.cell.style.top)  - y;
        return Math.abs(dx) >= min_dist || Math.abs(dy) >= min_dist;
      });
      if (ok) break;
    }
    this.cell.style.left = x + "px";
    this.cell.style.top  = y + "px";

    this.connect();
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);

    this.ws.onopen = () => {
      this.cell.classList.replace("ws-closed", "ws-open") || this.cell.classList.add("ws-open");
      this.ws.send(JSON.stringify({ type: "hello", device_id: this.deviceId }));
      this.ws.send(JSON.stringify({ type: "sync_ping", client_time: Date.now() }));
      setInterval(() => {
        if (this.ws.readyState === WebSocket.OPEN)
          this.ws.send(JSON.stringify({ type: "ping" }));
      }, 15000);
      updateWsStatus();
    };

    this.ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);

      if (msg.type === "shutdown") {
        this.mode = "WAITING";
        this.phases = [];
      }

      if (msg.type === "assigned") {
        this.blinkId = msg.blink_id;
        this.u       = msg.u ?? 0;
        this.v       = msg.v ?? 0;
        this.phases  = encodeId(this.blinkId);
        this.startMs = Date.now();
        this.mode    = "WAITING";
        this.idEl.textContent = this.blinkId;
      }

      if (msg.type === "sync_pong") {
        const now = Date.now();
        const rtt = now - msg.client_time;
        this.clockOffset = msg.server_time - (msg.client_time + rtt / 2);
      }

      if (msg.type === "update_position") {
        this.u = msg.u ?? this.u;
        this.v = msg.v ?? this.v;
        this.detected = true;
      }

      if (msg.type === "detection_started") {
        this.mode     = "DETECTION";
        this.detected = false;
      }

      if (msg.type === "detection_ended") {
        this.mode = "IDLE";
      }

      if (msg.type === "effect") {
        this.mode            = "SHOWTIME";
        this.currentEffect   = msg.effect;
        this.effectStartTime = msg.start_time;
        this.effectSpeed     = msg.speed ?? 0.3;
        this.effectSpatialFreq = msg.spatial_freq ?? 1.5;
        this.effectBpm       = msg.bpm ?? 100;
        this.effectAngle     = msg.angle ?? 0;
        this.effectR         = msg.color_r ?? 255;
        this.effectG         = msg.color_g ?? 255;
        this.effectB         = msg.color_b ?? 255;
        this.deviceOrder     = msg.device_order ?? [];
        this.sweepDwell      = msg.dwell ?? 0.18;
      }

      if (msg.type === "reset") {
        this.mode          = "WAITING";
        this.currentEffect = null;
        this.detected      = false;
      }
    };

    this.ws.onclose = () => {
      this.cell.classList.replace("ws-open", "ws-closed") || this.cell.classList.add("ws-closed");
      updateWsStatus();
      setTimeout(() => this.connect(), 1000);
    };
  }

  serverNow() { return Date.now() + this.clockOffset; }

  directedCoord(u, v) {
    const a = (this.effectAngle ?? 0) * Math.PI / 180;
    return (u * Math.cos(a) + v * Math.sin(a) + 1) / 2;
  }

  shade(u, v, t) {
    const e  = this.currentEffect;
    const d  = this.directedCoord(u, v);
    const er = this.effectR ?? 255;
    const eg = this.effectG ?? 255;
    const eb = this.effectB ?? 255;
    if (e === "wave") {
      const ph = 2 * Math.PI * (d * this.effectSpatialFreq - t * this.effectSpeed);
      const i  = 0.5 + 0.5 * Math.sin(ph);
      return `rgb(${i*er|0},${i*eg|0},${i*eb|0})`;
    }
    if (e === "gradient") {
      let i = (d - t * this.effectSpeed) % 1;
      if (i < 0) i += 1;
      return `rgb(${i*er|0},${i*eg|0},${i*eb|0})`;
    }
    if (e === "binary_wave") {
      const ph = 2 * Math.PI * (d * this.effectSpatialFreq - t * this.effectSpeed);
      const i  = Math.sin(ph) > 0 ? 1 : 0;
      return `rgb(${i*er|0},${i*eg|0},${i*eb|0})`;
    }
    if (e === "pulse") {
      const beat = Math.sin(2 * Math.PI * (this.effectBpm / 60) * t);
      const i = Math.max(0, beat);
      return `rgb(${i*er|0},${i*eg|0},${i*eb|0})`;
    }
    return "#000";
  }

  render() {
    if (this.mode !== "DETECTION" || this.phases.length === 0) {
      this.cell.style.background = "#000";
      this.cell.classList.remove("bright");
      return;
    }

    const totalMs  = this.phases.length * PHASE_MS;
    const phaseIdx = Math.floor((Date.now() - this.startMs) % totalMs / PHASE_MS);
    const phase    = this.phases[phaseIdx];

    this.cell.style.background = phase === 1 ? "#fff" : "#000";
    this.cell.classList.toggle("bright", phase === 1);
  }

  destroy() {
    if (this.ws) { this.ws.onclose = null; try { this.ws.close(); } catch {} this.ws = null; }
    this.cell.remove();
  }
}

function render() {
  clients.forEach(c => c.render());
  animFrame = requestAnimationFrame(render);
}

function start() {
  stopSim();
  grid.innerHTML = "";

  const n = Math.min(parseInt(document.getElementById("nInput").value) || 4, 32);
  for (let i = 0; i < n; i++) {
    clients.push(new SimClient(i));
  }

  animFrame = requestAnimationFrame(render);
  updateWsStatus();
}

function stopSim() {
  if (animFrame) cancelAnimationFrame(animFrame);
  clients.forEach(c => c.destroy());
  clients = [];
  updateWsStatus();
}
