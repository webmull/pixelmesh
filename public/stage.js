// (c) Adam Davis - adamdavis.co.uk
// PixelMesh V2 — Stage page renderer
//
// Standalone full-screen projected display.  Subscribes to /ws as a
// spectator and renders Diana & Rosie climbing ropes side-by-side using
// hand-drawn sprites packed in /public/assets/climb.png.

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
  diana:      0,
  rosie:      0,
  dianaDraw:  0,
  rosieDraw:  0,
  dianaRate:  0,        // height change per sec (smoothed) — used to pick climb frame
  rosieRate:  0,
  winner:     null,     // "diana" | "rosie" | "draw" | null
  winnerAt:   0,
  confetti:   [],       // array of {x, y, vx, vy, color, life, maxLife}
};

let lastDiana = 0, lastRosie = 0, lastUpdateT = performance.now();

function handleMessage(msg) {
  if (msg.type === "spectator_hello") {
    if (msg.game && msg.game.active) {
      game.active = true;
      game.diana  = (msg.game.heights || {}).diana || 0;
      game.rosie  = (msg.game.heights || {}).rosie || 0;
      lastDiana = game.diana; lastRosie = game.rosie;
    }
    return;
  }
  if (msg.type === "rope_start") {
    game.active = true;
    game.diana  = 0; game.rosie  = 0;
    game.dianaDraw = 0; game.rosieDraw = 0;
    game.dianaRate = 0; game.rosieRate = 0;
    game.winner = null;
    game.confetti = [];
    _confettiEmitAccum = 0;
    return;
  }
  if (msg.type === "rope_progress") {
    const now = performance.now();
    const dt  = Math.max(0.001, (now - lastUpdateT) / 1000);
    lastUpdateT = now;
    if (msg.diana != null) {
      game.dianaRate = ((msg.diana - lastDiana) / dt) * 0.4 + game.dianaRate * 0.6;
      lastDiana = msg.diana;
      game.diana = msg.diana;
    }
    if (msg.rosie != null) {
      game.rosieRate = ((msg.rosie - lastRosie) / dt) * 0.4 + game.rosieRate * 0.6;
      lastRosie = msg.rosie;
      game.rosie = msg.rosie;
    }
    return;
  }
  if (msg.type === "rope_end") {
    game.active = false;
    game.winner = msg.winner ?? "draw";
    game.winnerAt = performance.now();
    if (msg.diana != null) game.diana = msg.diana;
    if (msg.rosie != null) game.rosie = msg.rosie;
    game.confetti = [];
    _confettiEmitAccum = 0;
    _spawnConfetti(game.winner, 90, true);   // initial burst
    return;
  }
}

// ------------------------------------------------------------------ //
// Sprite sheet
// ------------------------------------------------------------------ //

const SHEET = new Image();
let sheetReady = false;
SHEET.onload  = () => { sheetReady = true; };
SHEET.onerror = () => { sheetReady = false; };
SHEET.src = "/public/assets/climb.png?v=1";

const SPRITES = {
  diana_portrait:  { x: 416, y:   0, w: 286, h: 335 },
  rosie_portrait:  { x: 824, y:  53, w: 296, h: 287 },
  diana_climb_a:   { x: 326, y: 367, w: 180, h: 443 },
  diana_climb_b:   { x: 505, y: 367, w: 175, h: 443 },
  rosie_climb_a:   { x: 850, y: 367, w: 165, h: 443 },
  rosie_climb_b:   { x:1010, y: 367, w: 170, h: 443 },
  tap_button:      { x: 532, y: 836, w: 145, h: 145 },
  winner_banner:   { x: 691, y: 836, w: 480, h: 188 },
};

// Horizontal centre of the rope WITHIN each climb sprite, as a fraction of
// the sprite's width.  Measured from the amber-rope pixels in climb.png so
// each climber's hands actually land on the stage rope when we draw at the
// rope's X position (rather than the sprite's centre, which left every
// climber visibly offset to one side).
const ROPE_ANCHOR_X = {
  diana_climb_a: 0.722,
  diana_climb_b: 0.663,
  rosie_climb_a: 0.400,
  rosie_climb_b: 0.329,
};

function drawSprite(ctx, name, dx, dy, dw, dh) {
  if (!sheetReady) return;
  const s = SPRITES[name];
  if (!s) return;
  // round to integer canvas pixels so pixel art stays crisp
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(
    SHEET, s.x, s.y, s.w, s.h,
    Math.round(dx), Math.round(dy), Math.round(dw), Math.round(dh),
  );
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
// Rope rendering — drawn only outside the sprite region so the sprite's
// own rope shows where the character actually grips it.
// ------------------------------------------------------------------ //

function drawRopeFrame(centerXScene, accentColor, t) {
  // Anchor beam at top
  const beamY = SCENE_H * 0.08;
  const beamH = 5;
  const beamW = 38;
  ctx.fillStyle = "#6b4d28";
  ctx.fillRect(sx(centerXScene - beamW / 2), sy(beamY), ss(beamW), ss(beamH));
  ctx.fillStyle = "#3a2a16";
  ctx.fillRect(sx(centerXScene - beamW / 2), sy(beamY + beamH - 1), ss(beamW), ss(1));

  // Continuous rope strand from the beam down to the floor.  The
  // character sprites carry their own rope graphics, but those only
  // cover the local stretch around the climber — without this strand
  // the rope looked cut off, visible only where the climber happens to
  // be.  The strand below uses the same yellow/amber/brown palette as
  // the sprite ropes so they tile seamlessly when the character covers
  // a section.
  const ropeTop    = beamY + beamH;
  const ropeBottom = SCENE_H * 0.86;
  const ropeW      = 5;
  // Subtle horizontal sway anchored to the beam, easing toward zero at
  // the top so the rope still meets the anchor cleanly.
  const swayBase = Math.sin(t * 0.0007 + centerXScene) * 1.2;
  const ropeXTop = centerXScene;
  // Main rope body (amber)
  ctx.fillStyle = "#b48438";
  ctx.fillRect(sx(ropeXTop - ropeW / 2), sy(ropeTop),
               ss(ropeW), ss(ropeBottom - ropeTop));
  // Shaded right edge
  ctx.fillStyle = "#7a5621";
  ctx.fillRect(sx(ropeXTop + ropeW / 2 - 1), sy(ropeTop),
               ss(1), ss(ropeBottom - ropeTop));
  // Lit left edge
  ctx.fillStyle = "#e3b96b";
  ctx.fillRect(sx(ropeXTop - ropeW / 2), sy(ropeTop),
               ss(1), ss(ropeBottom - ropeTop));
  // Twist marks — slow vertical scroll, gives the rope visible texture
  const twistSpacing = 6;
  const twistOffset  = ((t * 0.005) % twistSpacing);
  ctx.fillStyle = "#4a3416";
  for (let y = ropeTop + twistOffset; y < ropeBottom; y += twistSpacing) {
    ctx.fillRect(sx(ropeXTop - ropeW / 2 + 1), sy(y), ss(ropeW - 2), ss(1));
  }

  // Anchor "ring" attaching rope to beam — accent colour
  ctx.fillStyle = accentColor;
  ctx.fillRect(sx(centerXScene - 4), sy(beamY + beamH), ss(8), ss(3));
  // Top star floating above the beam
  drawStar(sx(centerXScene), sy(beamY - 7), ss(5), t);
  // Tiny mute on swayBase usage so linter doesn't complain
  void swayBase;
}

function drawStar(cx, cy, r, t) {
  // Pulse the star slightly
  const pulse = 1 + 0.18 * Math.sin(t * 0.005);
  const rr = r * pulse;
  ctx.fillStyle = "#ffe27a";
  ctx.fillRect(cx - rr / 8, cy - rr,    rr / 4, rr * 2);
  ctx.fillRect(cx - rr,     cy - rr / 8, rr * 2, rr / 4);
  ctx.fillStyle = "#fff4b0";
  ctx.fillRect(cx - rr / 3, cy - rr / 3, rr / 1.5, rr / 1.5);
  // Soft glow
  ctx.fillStyle = "rgba(255, 226, 122, 0.18)";
  ctx.fillRect(cx - rr * 1.8, cy - rr * 1.8, rr * 3.6, rr * 3.6);
}

// ------------------------------------------------------------------ //
// Characters
// ------------------------------------------------------------------ //

const CLIMB_BOTTOM_Y = 0.79;    // scene-fractional Y where feet sit at h=0
const CLIMB_TOP_Y    = 0.20;    // scene-fractional Y where head reaches at h=1
const SPRITE_HEIGHT  = 110;     // scene units — how tall each character sprite displays

function lerp(a, b, t) { return a + (b - a) * t; }

function drawCharacter(team, centerXScene, height, rate, t) {
  // Animation: alternate frame A/B at a rate proportional to climb rate.
  const climbing = rate > 0.004;     // ~0.004 height/sec ≈ active tapping
  const period = climbing ? Math.max(0.30, 0.55 - rate * 8) : 1.2;
  const frame = Math.floor((t * 0.001 / period) * 2) % 2;
  const name = team + "_climb_" + (frame === 0 ? "a" : "b");
  const sprite = SPRITES[name];
  const h = Math.max(0, Math.min(1, height));
  const baseY = lerp(CLIMB_BOTTOM_Y, CLIMB_TOP_Y, h) * SCENE_H;
  const bobAmp = climbing ? 0 : 1.4;
  const bobY   = bobAmp * Math.sin(t * 0.003 + (team === "diana" ? 0 : 1.6));
  const swayAmp = climbing ? 1.6 : 0;
  const swayX   = swayAmp * Math.sin(t * 0.012);
  const dispH = SPRITE_HEIGHT;
  const dispW = dispH * (sprite.w / sprite.h);
  // Anchor the sprite so the rope WITHIN it aligns with the stage rope at
  // centerXScene — without this, characters were drawn centred on the
  // rope, so the rope-within-sprite landed off to one side of the actual
  // stage rope.
  const anchorFrac = ROPE_ANCHOR_X[name] ?? 0.5;
  const x = (centerXScene + swayX) - dispW * anchorFrac;
  const y = baseY - dispH * 0.92 + bobY;
  drawSprite(ctx, name, sx(x), sy(y), ss(dispW), ss(dispH));
}

// ------------------------------------------------------------------ //
// HUD
// ------------------------------------------------------------------ //

function drawLabel(text, centerXScene, yScene, color, sizeScene, weight) {
  const size = Math.max(12, Math.round(sizeScene * drawScale));
  ctx.fillStyle    = color;
  ctx.font         = `${weight || 700} ${size}px -apple-system, system-ui, sans-serif`;
  ctx.textAlign    = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, sx(centerXScene), sy(yScene));
}

function drawHeader() {
  drawLabel("ROPE CLIMB", SCENE_W / 2, SCENE_H * 0.04,
            "rgba(255,255,255,0.42)", 9, 700);
}

function drawTeamLabels(t) {
  drawLabel("DIANA", SCENE_W * 0.30, SCENE_H * 0.95, "#6fdb96", 12, 800);
  drawLabel("ROSIE", SCENE_W * 0.70, SCENE_H * 0.95, "#f88aaa", 12, 800);
  // % below name
  drawLabel(Math.round(game.dianaDraw * 100) + "%",
            SCENE_W * 0.30, SCENE_H * 0.89,
            "rgba(255,255,255,0.55)", 9, 600);
  drawLabel(Math.round(game.rosieDraw * 100) + "%",
            SCENE_W * 0.70, SCENE_H * 0.89,
            "rgba(255,255,255,0.55)", 9, 600);
}

function drawWaitingState() {
  if (game.active || game.winner) return;
  // Show the two portrait sprites side by side as a "ready" tableau
  if (!sheetReady) return;
  const portraitH = 70;
  const dianaS = SPRITES.diana_portrait;
  const rosieS = SPRITES.rosie_portrait;
  const dianaW = portraitH * (dianaS.w / dianaS.h);
  const rosieW = portraitH * (rosieS.w / rosieS.h);
  drawSprite(ctx, "diana_portrait",
    sx(SCENE_W * 0.30 - dianaW / 2), sy(SCENE_H * 0.45 - portraitH / 2),
    ss(dianaW), ss(portraitH));
  drawSprite(ctx, "rosie_portrait",
    sx(SCENE_W * 0.70 - rosieW / 2), sy(SCENE_H * 0.45 - portraitH / 2),
    ss(rosieW), ss(portraitH));
  drawLabel("WAITING FOR ROPE CLIMB", SCENE_W / 2, SCENE_H * 0.72,
            "rgba(255,255,255,0.32)", 10, 700);
}

// ------------------------------------------------------------------ //
// Winner banner + confetti
// ------------------------------------------------------------------ //

function _confettiColours(winner) {
  return winner === "diana"
       ? ["#6fdb96", "#3cd073", "#ffd740", "#ffffff"]
       : winner === "rosie"
       ? ["#f88aaa", "#c34772", "#ffd740", "#ffffff"]
       : ["#ffd740", "#ffffff", "#88c8ff", "#f88aaa"];
}

function _spawnConfetti(winner, count, burst) {
  // burst=true for the initial blast (mid-screen explosion); false for the
  // ongoing trickle (gentler emit from above so it can fall through frame).
  const colours = _confettiColours(winner);
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
      game.confetti.push({
        x: Math.random() * SCENE_W,
        y: -10 - Math.random() * 30,
        vx: (Math.random() - 0.5) * 25,
        vy: 25 + Math.random() * 30,
        ay: 25,
        color: colours[(Math.random() * colours.length) | 0],
        life: 0,
        maxLife: 4 + Math.random() * 3,
        size: 2 + Math.random() * 3,
      });
    }
  }
}

let _confettiEmitAccum = 0;
function _maybeEmitConfetti(dt) {
  if (!game.winner) return;
  _confettiEmitAccum += dt;
  // Steady trickle of ~25 pieces per 0.6 s — enough to feel ongoing
  // without piling up.  Capped so a long-running winner screen doesn't
  // accumulate thousands of particles.
  if (_confettiEmitAccum >= 0.6) {
    _confettiEmitAccum = 0;
    if (game.confetti.length < 220) {
      _spawnConfetti(game.winner, 25, false);
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

function drawWinnerBanner(t) {
  if (!game.winner) return;
  const age = (t - game.winnerAt) / 1000;

  // Scene dim under the banner
  const dim = Math.min(0.72, age * 1.4);
  ctx.fillStyle = `rgba(0,0,0,${dim})`;
  ctx.fillRect(offX, offY, drawW, drawH);

  // Spinning radiant burst behind the banner — gives the celebration
  // some motion even when the sprite itself has finished settling.
  const burstPop = Math.min(1, age * 1.6);
  if (burstPop > 0) {
    const cx = sx(SCENE_W / 2);
    const cy = sy(SCENE_H * 0.42);
    const baseRadius = ss(SCENE_H * 0.35) * burstPop;
    const teamColor = game.winner === "diana" ? "rgba(110,219,150,"
                    : game.winner === "rosie" ? "rgba(248,138,170,"
                                              : "rgba(255,215,64,";
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(t * 0.0006);
    const rays = 14;
    for (let i = 0; i < rays; i++) {
      const ang = (i / rays) * Math.PI * 2;
      const len = baseRadius * (1 + 0.12 * Math.sin(t * 0.004 + i));
      ctx.save();
      ctx.rotate(ang);
      const grad = ctx.createLinearGradient(0, 0, len, 0);
      grad.addColorStop(0, teamColor + "0.18)");
      grad.addColorStop(1, teamColor + "0)");
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(0, -ss(4));
      ctx.lineTo(len, 0);
      ctx.lineTo(0, ss(4));
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
    ctx.restore();
  }

  // Banner sprite — pop in with overshoot, then continuous wobble + bob.
  const pop      = Math.min(1, age * 2.5);
  const popScale = pop < 1 ? 1 + 0.22 * (1 - pop) : 1;
  const settleT  = Math.max(0, age - 0.4);
  const wobble   = (1 - Math.min(1, settleT * 1.6)) * Math.sin(settleT * 12) * 0.05;
  const idleBob  = Math.sin(t * 0.0025) * 1.5;
  const idleTilt = Math.sin(t * 0.0017) * 0.025;

  const bannerH = 68;
  const bs = SPRITES.winner_banner;
  const bw = bannerH * (bs.w / bs.h);

  ctx.save();
  ctx.translate(sx(SCENE_W / 2), sy(SCENE_H * 0.42) + ss(idleBob));
  ctx.rotate(idleTilt + wobble * 0.6);
  ctx.scale(popScale * (1 + wobble), popScale * (1 - wobble));
  drawSprite(ctx, "winner_banner",
    -ss(bw / 2), -ss(bannerH / 2),
    ss(bw), ss(bannerH));
  ctx.restore();

  // Team label below the banner — fades in, then pulses gently.
  if (game.winner !== "draw") {
    const color = game.winner === "diana" ? "#6fdb96" : "#f88aaa";
    const teamLabel = "TEAM " + game.winner.toUpperCase();
    const labelPop = Math.min(1, Math.max(0, (age - 0.35) * 2.4));
    const pulse    = 1 + 0.06 * Math.sin(t * 0.006);
    const sz = 18 * pulse;
    ctx.globalAlpha = labelPop;
    drawLabel(teamLabel, SCENE_W / 2, SCENE_H * 0.64,
              color, sz, 900);
    ctx.globalAlpha = 1;
  }
}

// ------------------------------------------------------------------ //
// Main render loop
// ------------------------------------------------------------------ //

let lastT = performance.now();
function frame(now) {
  const dt = Math.min(0.1, (now - lastT) / 1000);
  lastT = now;

  // Ease the display height toward the server value
  const easeRate = 6;
  game.dianaDraw += (game.diana - game.dianaDraw) * Math.min(1, dt * easeRate);
  game.rosieDraw += (game.rosie - game.rosieDraw) * Math.min(1, dt * easeRate);

  _maybeEmitConfetti(dt);
  if (game.confetti.length) updateConfetti(dt);

  drawBackground(now);
  drawRopeFrame(SCENE_W * 0.30, "#6fdb96", now);
  drawRopeFrame(SCENE_W * 0.70, "#f88aaa", now);
  drawHeader();
  drawTeamLabels(now);

  if (game.active || game.winner) {
    drawCharacter("diana", SCENE_W * 0.30, game.dianaDraw, game.dianaRate, now);
    drawCharacter("rosie", SCENE_W * 0.70, game.rosieDraw, game.rosieRate, now);
  } else {
    drawWaitingState();
  }

  drawWinnerBanner(now);
  if (game.confetti.length) drawConfetti();

  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
