// (c) Adam Davis - adamdavis.co.uk
// PixelMesh V2 — Stage page renderer
//
// Standalone full-screen projected display.  Subscribes to /ws as a
// spectator and renders the Avatar Race — one procedurally-generated
// character per phone, sprinting along a horizontal track toward a
// chequered finish line.

// ------------------------------------------------------------------ //
// WebSocket
// ------------------------------------------------------------------ //

const wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
const spectatorId = "stage-" + Math.random().toString(36).slice(2, 10);
let ws = null;
const connBadge = document.getElementById("conn");

function connect() {
  try { ws = new WebSocket(wsUrl); }
  catch (e) { setTimeout(connect, 1500); return; }
  ws.onopen    = () => {
    connBadge.classList.remove("show");
    ws.send(JSON.stringify({ type: "hello", role: "spectator", device_id: spectatorId }));
  };
  ws.onmessage = ev => { try { handleMessage(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose   = () => { connBadge.classList.add("show"); setTimeout(connect, 1500); };
  ws.onerror   = () => { try { ws.close(); } catch (e) {} };
}
connect();

// ------------------------------------------------------------------ //
// Game state
// ------------------------------------------------------------------ //

const game = {
  active:     false,
  race: {
    runners:   [],          // [{ blink_id, pos, draw }] kept sorted by current pos
    winner:    null,        // blink_id | null
    winnerAt:  0,
  },
  confetti:   [],
};

function _raceEnsureRunner(bid) {
  let r = game.race.runners.find(r => r.blink_id === bid);
  if (!r) {
    r = { blink_id: bid, pos: 0, draw: 0 };
    game.race.runners.push(r);
  }
  return r;
}

function _raceApplyPositions(positions) {
  for (const [bidStr, pos] of Object.entries(positions || {})) {
    const r = _raceEnsureRunner(parseInt(bidStr, 10));
    r.pos = pos;
  }
}

function handleMessage(msg) {
  if (msg.type === "spectator_hello") {
    const g = msg.game || {};
    if (g.active) {
      game.active = true;
      game.race.runners = [];
      _raceApplyPositions(g.positions);
      for (const r of game.race.runners) r.draw = r.pos;
    }
    return;
  }
  if (msg.type === "race_start") {
    game.active  = true;
    game.race.runners = (msg.blink_ids || []).map(bid => ({
      blink_id: bid, pos: 0, draw: 0,
    }));
    game.race.winner   = null;
    game.race.winnerAt = 0;
    game.confetti = [];
    _confettiEmitAccum = 0;
    return;
  }
  if (msg.type === "race_progress") {
    _raceApplyPositions(msg.positions);
    return;
  }
  if (msg.type === "race_end") {
    game.active = false;
    game.race.winner   = msg.winner ?? null;
    game.race.winnerAt = performance.now();
    _raceApplyPositions(msg.positions);
    game.confetti = [];
    _confettiEmitAccum = 0;
    // No celebration on a manual stop (winner == null) — just clear state.
    if (game.race.winner != null) _spawnConfetti("race", 90, true);
    return;
  }
}

// ------------------------------------------------------------------ //
// Canvas / scene layout
// ------------------------------------------------------------------ //

const canvas = document.getElementById("stage");
const ctx    = canvas.getContext("2d", { alpha: false });

// Logical scene dimensions — characters and layout are computed in these
// units; everything is uniformly scaled to fit the viewport.
const SCENE_W = 480;
const SCENE_H = 270;
let drawScale = 1, offX = 0, offY = 0, drawW = SCENE_W, drawH = SCENE_H;

function fitCanvas() {
  const vw = window.innerWidth, vh = window.innerHeight;
  const scale = Math.max(1, Math.min(vw / SCENE_W, vh / SCENE_H));
  canvas.width  = vw;
  canvas.height = vh;
  drawScale = scale;
  drawW = SCENE_W * scale;
  drawH = SCENE_H * scale;
  offX  = Math.round((vw - drawW) / 2);
  offY  = Math.round((vh - drawH) / 2);
}
fitCanvas();
window.addEventListener("resize", fitCanvas);

function sx(x) { return offX + x * drawScale; }
function sy(y) { return offY + y * drawScale; }
function ss(v) { return v * drawScale; }

// ------------------------------------------------------------------ //
// Background
// ------------------------------------------------------------------ //

function drawBackground(t) {
  // Letterbox black borders
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  // Scene gradient sky
  const grad = ctx.createLinearGradient(0, offY, 0, offY + drawH);
  grad.addColorStop(0,    "#0a0e18");
  grad.addColorStop(0.55, "#101626");
  grad.addColorStop(1,    "#1a1310");
  ctx.fillStyle = grad;
  ctx.fillRect(offX, offY, drawW, drawH);
  // Subtle starfield twinkle — fixed positions, alpha oscillates
  ctx.fillStyle = "rgba(255,255,255,0.7)";
  for (let i = 0; i < 30; i++) {
    const x  = (i * 53.7) % SCENE_W;
    const y  = (i * 23.3) % (SCENE_H * 0.55);
    const phase = (t * 0.001 + i * 0.31) % (Math.PI * 2);
    const a  = 0.15 + 0.25 * (0.5 + 0.5 * Math.sin(phase * 2));
    ctx.globalAlpha = a;
    ctx.fillRect(sx(x), sy(y), Math.max(1, drawScale), Math.max(1, drawScale));
  }
  ctx.globalAlpha = 1;
  // Floor band
  const floorY = sy(SCENE_H * 0.84);
  ctx.fillStyle = "#0c0805";
  ctx.fillRect(offX, floorY, drawW, drawH - (floorY - offY));
  ctx.fillStyle = "rgba(255, 220, 180, 0.10)";
  ctx.fillRect(offX, floorY, drawW, Math.max(1, drawScale));
}

// ------------------------------------------------------------------ //
// HUD                                                                  //
// ------------------------------------------------------------------ //

function drawLabel(text, centerXScene, yScene, color, sizeScene, weight) {
  const size = Math.max(12, Math.round(sizeScene * drawScale));
  ctx.fillStyle    = color;
  ctx.font         = `${weight || 700} ${size}px -apple-system, system-ui, sans-serif`;
  ctx.textAlign    = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, sx(centerXScene), sy(yScene));
}

// ------------------------------------------------------------------ //
// Confetti                                                             //
// ------------------------------------------------------------------ //

function _confettiColours() {
  return ["#5bb1ff", "#ffd740", "#ffffff", "#b9e6ff", "#ff7eb6", "#9affb2"];
}

function _spawnConfetti(_winner, count, burst) {
  // burst=true for the initial blast (mid-screen explosion); false for the
  // ongoing trickle (gentler emit from above so it can fall through frame).
  const colours = _confettiColours();
  for (let i = 0; i < count; i++) {
    if (burst) {
      game.confetti.push({
        x: SCENE_W / 2 + (Math.random() - 0.5) * 200,
        y: SCENE_H * 0.5 + (Math.random() - 0.5) * 60,
        vx: (Math.random() - 0.5) * 90,
        vy: -40 - Math.random() * 90,
        ay: 60,
        color: colours[(Math.random() * colours.length) | 0],
        life: 0,
        maxLife: 3 + Math.random() * 2,
        size: 2 + Math.random() * 3,
      });
    } else {
      // Trickle from above the scene with a bit of horizontal drift and
      // gravity, so the room feels actively snowed-on with confetti.
      game.confetti.push({
        x: Math.random() * SCENE_W,
        y: -10 - Math.random() * 30,
        vx: (Math.random() - 0.5) * 40,
        vy: 35 + Math.random() * 45,
        ay: 45,
        color: colours[(Math.random() * colours.length) | 0],
        life: 0,
        maxLife: 4.5 + Math.random() * 2.5,
        size: 3 + Math.random() * 3,
      });
    }
  }
}

let _confettiEmitAccum = 0;
function _maybeEmitConfetti(dt) {
  // Only run the loop once a winner is announced.
  if (game.race.winner == null) return;
  _confettiEmitAccum += dt;
  // Heavy nonstop trickle so the celebration feels like an actual party
  // rather than a brief burst.  ~60 pieces every 0.15 s * ~5 s lifetime =
  // ~2000 particles in flight at steady state; hard-capped at 1200 so
  // we don't kill the GPU on a long winner screen.
  if (_confettiEmitAccum >= 0.15) {
    _confettiEmitAccum = 0;
    if (game.confetti.length < 1200) {
      _spawnConfetti("race", 60, false);
    }
  }
}

function updateConfetti(dt) {
  for (const p of game.confetti) {
    p.life += dt;
    p.vx += p.ax * dt;
    p.vy += p.ay * dt;
    p.x  += p.vx * dt;
    p.y  += p.vy * dt;
  }
  game.confetti = game.confetti.filter(p => p.life < p.maxLife);
}

function drawConfetti() {
  for (const p of game.confetti) {
    const lifeFrac = p.life / p.maxLife;
    ctx.globalAlpha = Math.max(0, 1 - lifeFrac);
    ctx.fillStyle = p.color;
    ctx.fillRect(sx(p.x), sy(p.y), ss(p.size), ss(p.size));
  }
  ctx.globalAlpha = 1;
}


// ------------------------------------------------------------------ //
// Avatar race                                                          //
// ------------------------------------------------------------------ //

// Wang integer hash → float [0, 1).  Used to seed per-phone avatar
// features so each runner gets a deterministic, distinct character
// without server-side state.
function _hashF(n) {
  n = ((n ^ 61) ^ (n >>> 16)) >>> 0;
  n = ((n + (n << 3)) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 4)) >>> 0;
  n = ((n * 0x27D4EB2D) & 0x7FFFFFFF) >>> 0;
  n =  (n ^ (n >>> 15)) >>> 0;
  return (n & 0x7FFFFFFF) / 0x7FFFFFFF;
}

function _hsl(h, s, l) {
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

const SKIN_TONES = ["#f1c9a5", "#d9a07e", "#a87049", "#6d4524"];
const HAT_STYLES = ["beanie", "cap", "top", "none"];

function _avatarFeatures(bid) {
  return {
    bodyHue:   _hashF(bid),
    hatHue:    _hashF(bid * 7 + 11),
    skin:      SKIN_TONES[Math.floor(_hashF(bid * 13 + 5) * SKIN_TONES.length)],
    hatStyle:  HAT_STYLES[Math.floor(_hashF(bid * 23 + 3) * HAT_STYLES.length)],
  };
}

function drawAvatar(cx, cy, sizePx, bid, running, runningPhase) {
  // sizePx = total avatar height in canvas pixels.  Avatar = head + body.
  const f = _avatarFeatures(bid);
  const headR = sizePx * 0.22;
  const bodyW = sizePx * 0.45;
  const bodyH = sizePx * 0.50;
  const headCy = cy - sizePx * 0.28;
  // Body bob when running (alternating up/down)
  const bob = running ? Math.sin(runningPhase) * sizePx * 0.04 : 0;
  // Body
  ctx.fillStyle = _hsl(f.bodyHue, 0.75, 0.5);
  ctx.fillRect(cx - bodyW / 2, cy - bodyH / 2 + bob, bodyW, bodyH);
  // Arms (suggested) — narrow rectangles on the sides
  ctx.fillStyle = f.skin;
  const armW = sizePx * 0.08;
  ctx.fillRect(cx - bodyW / 2 - armW, cy - bodyH * 0.35 + bob, armW, bodyH * 0.5);
  ctx.fillRect(cx + bodyW / 2,        cy - bodyH * 0.35 + bob, armW, bodyH * 0.5);
  // Legs (alternating when running)
  const legW = sizePx * 0.12;
  const legH = sizePx * 0.20;
  const legY = cy + bodyH / 2 + bob;
  const legSwing = running ? Math.sin(runningPhase) * sizePx * 0.08 : 0;
  ctx.fillStyle = _hsl(f.bodyHue, 0.45, 0.25);
  ctx.fillRect(cx - bodyW * 0.35 - legW / 2, legY,            legW, legH - Math.abs(legSwing));
  ctx.fillRect(cx + bodyW * 0.35 - legW / 2, legY,            legW, legH - Math.abs(-legSwing));
  // Head
  ctx.fillStyle = f.skin;
  ctx.beginPath();
  ctx.arc(cx, headCy + bob, headR, 0, Math.PI * 2);
  ctx.fill();
  // Hat
  if (f.hatStyle !== "none") {
    ctx.fillStyle = _hsl(f.hatHue, 0.85, 0.45);
    if (f.hatStyle === "beanie") {
      ctx.beginPath();
      ctx.arc(cx, headCy - headR * 0.2 + bob, headR * 1.05, Math.PI, 0);
      ctx.fill();
    } else if (f.hatStyle === "cap") {
      ctx.fillRect(cx - headR, headCy - headR * 0.5 + bob, headR * 2, headR * 0.45);
      ctx.fillRect(cx - headR * 0.2, headCy - headR * 0.2 + bob, headR * 1.6, headR * 0.18);
    } else if (f.hatStyle === "top") {
      ctx.fillRect(cx - headR * 0.7, headCy - headR * 1.6 + bob, headR * 1.4, headR * 1.1);
      ctx.fillRect(cx - headR * 1.1, headCy - headR * 0.5 + bob, headR * 2.2, headR * 0.18);
    }
  }
}

function drawRaceTrack(t) {
  const N = game.race.runners.length;
  if (N === 0) {
    drawLabel("WAITING FOR RACE", SCENE_W / 2, SCENE_H * 0.5,
              "rgba(255,255,255,0.32)", 14, 700);
    return;
  }
  // Track area inside the scene
  const trackX0 = SCENE_W * 0.07;
  const trackX1 = SCENE_W * 0.92;
  const trackY0 = SCENE_H * 0.14;
  const trackY1 = SCENE_H * 0.86;
  const trackW  = trackX1 - trackX0;
  const trackH  = trackY1 - trackY0;
  // One lane per runner — divide evenly so the field always fits inside
  // the track regardless of roster size (70+ phones still pack cleanly).
  const laneH      = trackH / N;
  const avatarSize = Math.min(laneH * 0.95, 26);
  // Background track lines
  ctx.fillStyle = "rgba(255,255,255,0.04)";
  ctx.fillRect(sx(trackX0), sy(trackY0), ss(trackW), ss(trackH));
  // Start + finish lines
  ctx.strokeStyle = "rgba(255,255,255,0.20)";
  ctx.lineWidth = Math.max(1, ss(0.5));
  ctx.beginPath();
  ctx.moveTo(sx(trackX0), sy(trackY0));
  ctx.lineTo(sx(trackX0), sy(trackY1));
  ctx.stroke();
  // Finish line — chequered band
  const finishW = Math.max(2, ss(3));
  const blockH  = Math.max(2, ss(4));
  for (let y = sy(trackY0); y < sy(trackY1); y += blockH * 2) {
    ctx.fillStyle = "#fff";
    ctx.fillRect(sx(trackX1) - finishW, y,          finishW / 2, blockH);
    ctx.fillRect(sx(trackX1) - finishW / 2, y + blockH, finishW / 2, blockH);
    ctx.fillStyle = "#222";
    ctx.fillRect(sx(trackX1) - finishW, y + blockH, finishW / 2, blockH);
    ctx.fillRect(sx(trackX1) - finishW / 2, y,          finishW / 2, blockH);
  }
  // Sort runners by descending position so leaders draw last (on top).
  const sorted = [...game.race.runners].sort((a, b) => a.draw - b.draw);
  for (let i = 0; i < sorted.length; i++) {
    const r = sorted[i];
    const lane = game.race.runners.findIndex(rr => rr.blink_id === r.blink_id);
    const laneY = trackY0 + (lane + 0.5) * laneH;
    const xScene = trackX0 + r.draw * trackW;
    const cx = sx(xScene);
    const cy = sy(laneY);
    // Lane line behind avatar — subtle horizontal track
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.beginPath();
    ctx.moveTo(sx(trackX0), cy);
    ctx.lineTo(sx(trackX1), cy);
    ctx.stroke();
    // Running animation phase from this runner's progress
    const running = game.active && r.draw < 1.0;
    const runningPhase = t * 0.012 + r.blink_id * 0.7;
    drawAvatar(cx, cy, ss(avatarSize), r.blink_id, running, runningPhase);
    // Phone number label to the right of the avatar
    if (avatarSize >= 18) {
      drawLabel(`#${r.blink_id + 1}`, xScene + 3.5, laneY,
                "rgba(255,255,255,0.55)", 7, 700);
    }
  }
}

function drawRaceHeader() {
  drawLabel("AVATAR RACE", SCENE_W / 2, SCENE_H * 0.05,
            "rgba(255,255,255,0.42)", 9, 700);
}

function drawRaceWinnerBanner(t) {
  if (game.race.winner == null) return;
  const age = (t - game.race.winnerAt) / 1000;

  // Soft dim under the celebration so the winner reads against any
  // residual track ghosts behind it.
  const dim = Math.min(0.55, age * 1.4);
  ctx.fillStyle = `rgba(0,0,0,${dim})`;
  ctx.fillRect(offX, offY, drawW, drawH);

  // Pop the WINNER text in with a gentle overshoot, then breathe.
  const pop      = Math.min(1, age * 2.5);
  const popScale = pop < 1 ? 1 + 0.30 * (1 - pop) : 1;
  const breathe  = 1 + 0.04 * Math.sin(t * 0.005);
  const wbid     = game.race.winner;

  ctx.save();
  ctx.translate(sx(SCENE_W / 2), sy(SCENE_H * 0.38));
  ctx.scale(popScale * breathe, popScale * breathe);

  // "WINNER" label
  const winnerSize = Math.max(18, Math.round(34 * drawScale));
  ctx.fillStyle    = "#ffd740";
  ctx.font         = `900 ${winnerSize}px -apple-system, system-ui, sans-serif`;
  ctx.textAlign    = "center";
  ctx.textBaseline = "middle";
  ctx.shadowColor  = "rgba(255,215,64,0.45)";
  ctx.shadowBlur   = winnerSize * 0.4;
  ctx.fillText("WINNER", 0, 0);
  ctx.shadowBlur   = 0;

  // Phone number underneath
  const phoneSize = Math.max(14, Math.round(22 * drawScale));
  ctx.fillStyle   = "#fff";
  ctx.font        = `800 ${phoneSize}px -apple-system, system-ui, sans-serif`;
  ctx.fillText(`PHONE #${wbid + 1}`, 0, winnerSize * 0.95);

  ctx.restore();

  // Winner's avatar — running animation under the text.
  const avSize = 64;
  drawAvatar(sx(SCENE_W / 2), sy(SCENE_H * 0.74),
             ss(avSize), wbid, true, t * 0.02);
}

// ------------------------------------------------------------------ //
// Main render loop
// ------------------------------------------------------------------ //

let lastT = performance.now();
function frame(now) {
  const dt = Math.min(0.1, (now - lastT) / 1000);
  lastT = now;

  // Ease each runner's drawn x toward the server position.
  const easeRate = 6;
  for (const r of game.race.runners) {
    r.draw += (r.pos - r.draw) * Math.min(1, dt * easeRate);
  }

  _maybeEmitConfetti(dt);
  if (game.confetti.length) updateConfetti(dt);

  drawBackground(now);
  drawRaceHeader();
  drawRaceTrack(now);
  drawRaceWinnerBanner(now);
  if (game.confetti.length) drawConfetti();

  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
