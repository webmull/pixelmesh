// PixelMesh V2 — Simulator
// Spawns N fake clients in the browser, each connecting via WebSocket
// and blinking their assigned blink_id.
// Useful for testing the server and the blink detection pipeline.

const NUM_BITS  = 5;
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
    this.mode       = "DETECTION";
    this.u          = 0;
    this.v          = 0;
    this.clockOffset = 0;
    this.detected   = false;
    this.detectedAt = 0;

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

      if (msg.type === "assigned") {
        this.blinkId = msg.blink_id;
        this.u       = msg.u ?? 0;
        this.v       = msg.v ?? 0;
        this.phases  = encodeId(this.blinkId);
        this.startMs = Date.now();
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
        this.detected   = true;
        this.detectedAt = Date.now();
      }

      if (msg.type === "mode") {
        this.mode = msg.mode;
      }

      if (msg.type === "effect") {
        this.mode           = "SHOWTIME";
        this.currentEffect  = msg.effect;
        this.effectStartTime = msg.start_time;
        this.effectSpeed    = msg.speed ?? 0.3;
        this.effectSpatialFreq = msg.spatial_freq ?? 1.5;
        this.effectBpm      = msg.bpm ?? 100;
        this.effectOriginU  = msg.origin_u ?? 0.5;
        this.effectOriginV  = msg.origin_v ?? 0.5;
        if (msg.device_order) {
          this.deviceOrder = msg.device_order;
          this.sweepDwell  = msg.dwell ?? 0.18;
        }
      }

      if (msg.type === "reset") {
        this.mode = "DETECTION";
        this.currentEffect = null;
        this.detected = false;
      }
    };

    this.ws.onclose = () => {
      this.cell.classList.replace("ws-open", "ws-closed") || this.cell.classList.add("ws-closed");
      updateWsStatus();
      setTimeout(() => this.connect(), 1000);
    };
  }

  serverNow() { return Date.now() + this.clockOffset; }

  shade(u, v, t) {
    const e = this.currentEffect;
    if (e === "wave") {
      const ph = 2 * Math.PI * (u * this.effectSpatialFreq - t * this.effectSpeed);
      const i  = 0.5 + 0.5 * Math.sin(ph);
      return `rgb(${i*255|0},${i*255|0},${i*255|0})`;
    }
    if (e === "gradient") {
      let i = (u - t * this.effectSpeed) % 1;
      if (i < 0) i += 1;
      return `rgb(${i*255|0},${i*255|0},${i*255|0})`;
    }
    if (e === "pulse") {
      const beat = Math.sin(2 * Math.PI * (this.effectBpm / 60) * t);
      const i = Math.max(0, beat);
      return `rgb(${i*255|0},${i*255|0},${i*255|0})`;
    }
    if (e === "click_ripple") {
      const dx = u - this.effectOriginU, dy = v - this.effectOriginV;
      const dist = Math.sqrt(dx*dx + dy*dy);
      const dur  = 4.0, prog = (t % dur) / dur;
      const wf   = prog * 1.4;
      const rw   = 0.04;
      const delta = dist - wf;
      const ring  = Math.exp(-(delta*delta)/rw);
      const echo  = 0.5 * Math.exp(-((dist-(wf-0.18))**2)/(rw*1.8));
      const i = Math.max(0, Math.min(1, (ring+echo)*Math.exp(-dist*1.2)));
      return `rgb(${i*40|0},${i*170|0},${i*255|0})`;
    }
    return "#000";
  }

  render() {
    if (this.mode === "SHOWTIME" && this.currentEffect) {
      const t   = (this.serverNow() - this.effectStartTime) / 1000;
      this.cell.style.background = this.shade(this.u, this.v, t);
      this.cell.classList.remove("bright");
      return;
    }

    // Detection mode — blink
    if (this.phases.length === 0) {
      this.cell.style.background = "#111";
      this.cell.classList.remove("bright");
      return;
    }

    const totalMs  = this.phases.length * PHASE_MS;
    const elapsed  = Date.now() - this.startMs;
    const phaseIdx = Math.floor((elapsed % totalMs) / PHASE_MS);
    const phase    = this.phases[phaseIdx];

    if (this.detected) {
      const age   = (Date.now() - this.detectedAt) / 1000;
      const pulse = Math.exp(-age * 0.6);
      const blink = phase === 1 ? 1.0 : 0.0;
      const r = Math.round(255 * (blink * (1 - pulse) + pulse));
      const g = Math.round(140 * blink * (1 - pulse) + 80 * pulse);
      this.cell.style.background = `rgb(${r},${g},0)`;
      this.cell.classList.remove("bright");
      if (age > 10) this.detected = false;
    } else {
      this.cell.style.background = phase === 1 ? "#fff" : "#000";
      this.cell.classList.toggle("bright", phase === 1);
    }
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
