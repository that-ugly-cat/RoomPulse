/* Donut — endless runner a un tasto (tap/spacebar). Gatto Donut, case, pterodattili.
   Sprite disegnati in codice a blocchi (unità 3px), silhouette curate. Velocità a rampa.
   API:  const g = Donut.mount(container, { texts, onGameOver });  g.destroy(); */
(function () {
  const U = 2;                              // dimensione del "pixellone" (fine)
  const H = 220, GROUND = 178, CATX = 60;
  const DIM = {                              // dimensioni sprite in unità
    cat: { w: 26, h: 20 }, houseS: { w: 18, h: 16 },
    houseL: { w: 24, h: 22 }, ptero: { w: 22, h: 20 },
    donut: { w: 8, h: 7 },
  };
  // skyline di rovine sullo sfondo (parallasse). Pattern ripetuto, larghezza SKYLINE_TILE.
  const SKYLINE_TILE = 420;
  const SKYLINE = [
    { x: 20, w: 26, h: 46 }, { x: 96, w: 30, h: 62 }, { x: 172, w: 22, h: 34 },
    { x: 242, w: 30, h: 70 }, { x: 322, w: 24, h: 40 }, { x: 382, w: 20, h: 30 },
  ];
  const CAT_W = DIM.cat.w * U, CAT_H = DIM.cat.h * U;

  function blk(ctx, x, y, gx, gy, gw, gh, c) {
    ctx.fillStyle = c; ctx.fillRect(x + gx * U, y + gy * U, gw * U, gh * U);
  }

  function drawCat(ctx, x, y, frame, air) {
    const o = "#e8862e", O = "#b5641a", w = "#f7ecd6", k = "#241608", p = "#e8708e",
      yy = "#ffd23f", Y = "#d9a520", j = "#e83a6a";
    const b = (gx, gy, gw, gh, c) => blk(ctx, x, y, gx, gy, gw, gh, c);
    b(1, 10, 2, 4, o); b(1, 6, 2, 4, o); b(2, 4, 2, 2, o); b(0, 7, 1, 4, O);   // coda
    b(4, 9, 14, 7, o); b(5, 8, 12, 1, o); b(6, 14, 11, 2, w);                   // corpo + pancia
    b(16, 6, 9, 9, o);                                                          // testa
    b(16, 2, 2, 4, o); b(17, 3, 1, 2, o); b(16, 2, 1, 1, O);                    // orecchio sx
    b(23, 2, 2, 4, o); b(23, 3, 1, 2, o); b(24, 2, 1, 1, O);                    // orecchio dx
    b(17, 4, 1, 1, p); b(23, 4, 1, 1, p);                                       // interni
    b(18, 5, 6, 1, yy); b(18, 6, 6, 1, Y);                                      // tiara: banda
    b(18, 4, 1, 1, yy); b(20, 3, 2, 2, yy); b(23, 4, 1, 1, yy); b(20, 4, 1, 1, j); // punte + gemma
    b(20, 8, 1, 2, k); b(22, 10, 3, 2, w); b(24, 10, 1, 1, p); b(22, 12, 2, 1, O);  // muso
    if (air) { b(6, 15, 3, 3, O); b(16, 15, 3, 3, O); }
    else if (frame === 0) { b(5, 15, 2, 4, o); b(8, 15, 2, 3, O); b(15, 15, 2, 4, o); b(18, 15, 2, 3, O); }
    else { b(6, 15, 2, 3, O); b(9, 15, 2, 4, o); b(16, 15, 2, 3, O); b(19, 15, 2, 4, o); }
  }

  function drawHouseS(ctx, x, y) {
    // rudere: muri sbiaditi, tetto sventrato, finestre spente
    const wall = "#8a7d6c", roof = "#463b34", hole = "#241d19",
      door = "#211b17", winD = "#2b241f", glow = "#6f7a3e", crack = "#5f5648";
    const b = (gx, gy, gw, gh, c) => blk(ctx, x, y, gx, gy, gw, gh, c);
    b(0, 5, 18, 1, roof); b(2, 4, 14, 1, roof); b(4, 3, 10, 1, roof); b(7, 2, 4, 1, roof); // tetto senza punta (crollato)
    b(1, 6, 16, 10, wall);
    b(9, 4, 3, 1, hole);                             // squarcio nel tetto
    b(7, 11, 4, 5, door);
    b(3, 8, 3, 3, winD); b(12, 8, 3, 3, glow);       // finestra spenta + una col bagliore malato
    b(5, 10, 1, 6, crack); b(14, 12, 1, 4, crack);   // crepe
  }

  function drawHouseL(ctx, x, y) {
    const wall = "#7c7060", roof = "#3f352f", hole = "#211a16",
      door = "#1b1512", winD = "#28221d", glow = "#6f7a3e", crack = "#544a3f";
    const b = (gx, gy, gw, gh, c) => blk(ctx, x, y, gx, gy, gw, gh, c);
    b(0, 6, 24, 1, roof); b(2, 5, 20, 1, roof); b(5, 4, 9, 1, roof); b(15, 4, 4, 1, roof); // colmo spezzato (gap)
    b(8, 3, 4, 1, roof);                                                                    // moncone superstite
    b(1, 7, 22, 15, wall);
    b(12, 5, 3, 2, hole);                             // squarcio nel tetto
    b(9, 16, 6, 6, door);
    b(4, 9, 4, 3, winD); b(16, 9, 4, 3, glow); b(4, 14, 3, 3, winD); b(17, 14, 3, 3, winD);
    b(11, 8, 1, 7, crack); b(6, 12, 1, 6, crack);    // crepe
  }

  function drawPtero(ctx, x, y, frame) {
    // faccia a sinistra. Becco+cresta a sinistra, grande ala a membrana che sbatte.
    const g = "#6a8a4a", G = "#46602f", beak = "#e8b84a", k = "#22301c";
    const b = (gx, gy, gw, gh, c) => blk(ctx, x, y, gx, gy, gw, gh, c);
    b(0, 10, 4, 2, beak); b(3, 9, 2, 1, beak); b(3, 12, 2, 1, beak);            // becco
    b(5, 9, 4, 4, g); b(6, 10, 2, 2, k);                                        // testa + occhio
    b(4, 7, 3, 1, g); b(2, 6, 3, 1, G);                                         // cresta
    b(9, 10, 6, 3, g);                                                          // corpo
    b(15, 10, 4, 1, g); b(19, 9, 2, 3, G);                                      // coda
    if (frame === 0) {                                                          // ala su
      b(9, 9, 12, 1, g); b(11, 8, 10, 1, g); b(13, 7, 8, 1, g); b(15, 6, 6, 1, g); b(17, 5, 4, 1, g); b(20, 4, 2, 1, G);
    } else {                                                                    // ala giù
      b(9, 13, 12, 1, g); b(11, 14, 10, 1, g); b(13, 15, 8, 1, g); b(15, 16, 6, 1, g); b(17, 17, 4, 1, g); b(20, 18, 2, 1, G);
    }
  }

  function drawDonut(ctx, x, y) {
    // ciambella (Princess Donut): glassa rosa, hole, zuccherini. Box 8×7.
    const g = "#f7a0c0", G = "#e06a97", s1 = "#ffffff", s2 = "#8fe0ff", s3 = "#ffe14d";
    const b = (gx, gy, gw, gh, c) => blk(ctx, x, y, gx, gy, gw, gh, c);
    b(3, 0, 3, 1, g);                                   // glassa: corona (torus)
    b(2, 1, 5, 1, g);
    b(1, 2, 2, 1, g); b(6, 2, 2, 1, g);
    b(1, 3, 2, 1, g); b(6, 3, 2, 1, g);
    b(1, 4, 2, 1, g); b(6, 4, 2, 1, g);
    b(2, 5, 5, 1, g);
    b(3, 6, 3, 1, G); b(2, 5, 1, 1, G); b(6, 5, 1, 1, G);  // ombra inferiore
    b(3, 1, 1, 1, s1); b(5, 1, 1, 1, s3); b(1, 3, 1, 1, s2);  // zuccherini
    b(6, 2, 1, 1, s1); b(4, 5, 1, 1, s3); b(2, 4, 1, 1, s1);
  }

  function hit(a, b) {
    const m = 5;
    return a.x + m < b.x + b.w && a.x + a.w - m > b.x &&
           a.y + m < b.y + b.h && a.y + a.h - m > b.y;
  }
  function grab(a, d) {                          // AABB pieno: hitbox generosa per le ciambelle
    return a.x < d.x + d.w && a.x + a.w > d.x &&
           a.y < d.y + d.h && a.y + a.h > d.y;
  }

  Donut.mount = function (container, opts) {
    opts = opts || {};
    const T = opts.texts || {};
    container.innerHTML = "";
    const cv = document.createElement("canvas");
    cv.height = H; cv.style.width = "100%"; cv.style.display = "block";
    cv.style.imageRendering = "pixelated"; cv.style.borderRadius = "10px";
    cv.style.touchAction = "manipulation";
    container.appendChild(cv);
    const ctx = cv.getContext("2d");
    ctx.imageSmoothingEnabled = false;

    let W = 0;
    function resize() { W = Math.max(320, container.clientWidth); cv.width = W; }
    resize();
    const ro = new ResizeObserver(resize); ro.observe(container);

    let state = "ready", vy = 0, catY = GROUND, grounded = true;
    let speed = 6, dist = 0, obstacles = [], frame = 0, spawnIn = 40, lastScore = 0;
    let bonus = 0, donuts = [], dSpawnIn = 90, floaters = [];
    let clouds = [{ x: 0.3, y: 40 }, { x: 0.7, y: 66 }].map(c => ({ x: c.x, y: c.y }));

    const score = () => Math.floor(dist / 10) + bonus;

    function reset() {
      vy = 0; catY = GROUND; grounded = true; speed = 6; dist = 0;
      obstacles = []; frame = 0; spawnIn = 40; state = "run";
      bonus = 0; donuts = []; dSpawnIn = 90; floaters = [];
    }
    function jump() {
      if (state !== "run") { reset(); return; }
      if (grounded) { vy = -13.2; grounded = false; }
    }
    function spawn() {
      const r = Math.random();
      const type = r < 0.42 ? "houseS" : r < 0.68 ? "houseL" : "ptero";
      const d = DIM[type];
      let yy;
      if (type === "ptero") {
        const ph = d.h * U, low = Math.random() < 0.6;
        yy = low ? GROUND - ph - 6 : GROUND - ph - 60;
      } else yy = GROUND + 3 - d.h * U;   // le case poggiano sulla superficie del terreno
      obstacles.push({ type, x: W + 10, y: yy, w: d.w * U, h: d.h * U });
    }

    function step() {
      frame++;
      vy += 0.8; catY += vy;
      if (catY >= GROUND) { catY = GROUND; vy = 0; grounded = true; }
      dist += speed; speed = 6 + dist / 1400;
      if (--spawnIn <= 0) { spawn(); spawnIn = Math.max(46, 92 - speed * 3) + Math.random() * 55; }
      if (--dSpawnIn <= 0) {                       // ciambelle: timer indipendente, altezza raggiungibile col salto
        const dh = DIM.donut.h * U;
        donuts.push({ x: W + 10, y: GROUND - dh - (44 + Math.random() * 60), w: DIM.donut.w * U, h: dh });
        dSpawnIn = 150 + Math.random() * 160;
      }
      const cat = { x: CATX, y: catY - CAT_H, w: CAT_W, h: CAT_H };
      for (const o of obstacles) {
        o.x -= speed;
        // per il ptero la hitbox è la banda del corpo (non la punta d'ala che sbatte)
        const box = o.type === "ptero"
          ? { x: o.x + 2 * U, y: o.y + 9 * U, w: 17 * U, h: 5 * U }
          : o;
        if (hit(cat, box)) return gameOver();
      }
      obstacles = obstacles.filter(o => o.x > -90);
      for (const dn of donuts) {
        dn.x -= speed;
        if (!dn.got && grab(cat, dn)) {            // presa: +100 e "+100" fluttuante
          dn.got = true; bonus += 100;
          floaters.push({ x: dn.x + dn.w / 2, y: dn.y, life: 32 });
        }
      }
      donuts = donuts.filter(dn => dn.x > -40 && !dn.got);
      for (const f of floaters) { f.y -= 1; f.life--; }
      floaters = floaters.filter(f => f.life > 0);
    }

    function drawBg() {
      // cielo cupo: gradiente verso l'orizzonte di smog
      const sky = ctx.createLinearGradient(0, 0, 0, GROUND);
      sky.addColorStop(0, "#655751"); sky.addColorStop(0.55, "#9a8977"); sky.addColorStop(1, "#c9b295");
      ctx.fillStyle = sky; ctx.fillRect(0, 0, W, GROUND + 3);
      // skyline di rovine (parallasse lenta), rado e semitrasparente per restare sullo sfondo
      const off = ((dist * 0.4) % SKYLINE_TILE + SKYLINE_TILE) % SKYLINE_TILE;
      ctx.globalAlpha = 0.4; ctx.fillStyle = "#544b47";
      for (let base = -SKYLINE_TILE; base < W + SKYLINE_TILE; base += SKYLINE_TILE) {
        for (let i = 0; i < SKYLINE.length; i++) {
          const bd = SKYLINE[i], bx = Math.round(base + bd.x - off);
          if (bx > W || bx + bd.w < 0) continue;
          const by = GROUND + 3 - bd.h;
          ctx.fillRect(bx, by + 5, bd.w, bd.h - 5);
          const seg = Math.max(4, (bd.w / 3) | 0);
          for (let s = 0, sx = bx; sx < bx + bd.w; s++, sx += seg) {
            const sh = 6 - ((i + s) % 3) * 2;                   // 6/4/2 → profilo spezzato
            ctx.fillRect(sx, by + 5 - sh, Math.min(seg - 1, bx + bd.w - sx), sh + 1);
          }
        }
      }
      ctx.globalAlpha = 1;
      // nubi di smog
      ctx.fillStyle = "#7d7167";
      for (const c of clouds) {
        const cx = ((c.x * W - dist * 0.25) % (W + 80) + W + 80) % (W + 80) - 40;
        ctx.fillRect(cx, c.y, 42, 9); ctx.fillRect(cx + 9, c.y - 6, 24, 9);
      }
      // terreno morto + detriti
      ctx.fillStyle = "#584b3f"; ctx.fillRect(0, GROUND + 3, W, H - GROUND);
      ctx.fillStyle = "#40382f";
      for (let x = -(dist % 24); x < W; x += 24) { ctx.fillRect(x, GROUND + 3, 12, 4); ctx.fillRect(x + 15, GROUND + 10, 6, 3); }
    }
    function drawObstacle(o) {
      if (o.type === "houseS") drawHouseS(ctx, o.x, o.y);
      else if (o.type === "houseL") drawHouseL(ctx, o.x, o.y);
      else drawPtero(ctx, o.x, o.y, (frame >> 3) & 1);
    }
    function drawScene() {
      drawBg();
      for (const dn of donuts) drawDonut(ctx, dn.x, dn.y);
      drawCat(ctx, CATX, catY - CAT_H, (frame >> 3) & 1, state === "run" && !grounded);
      for (const o of obstacles) drawObstacle(o);
      ctx.textAlign = "center"; ctx.font = "bold 16px monospace";
      for (const f of floaters) {
        ctx.globalAlpha = Math.min(1, f.life / 16); ctx.fillStyle = "#e0559a";
        ctx.fillText("+100", f.x, f.y);
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#33475a"; ctx.font = "bold 20px monospace"; ctx.textAlign = "right";
      ctx.fillText(String(score()).padStart(5, "0"), W - 12, 28);
    }
    function overlay(a, b) {
      ctx.fillStyle = "rgba(20,25,35,.55)"; ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = "#fff"; ctx.textAlign = "center";
      ctx.font = "bold 22px monospace"; ctx.fillText(a, W / 2, H / 2 - 6);
      if (b) { ctx.font = "14px monospace"; ctx.fillText(b, W / 2, H / 2 + 20); }
    }
    function gameOver() { state = "over"; lastScore = score(); if (opts.onGameOver) opts.onGameOver(lastScore); }

    let raf;
    function loop() {
      if (state === "run") { step(); drawScene(); }
      else if (state === "ready") { drawScene(); overlay(T.tap || "Tocca / spazio per iniziare"); }
      else { drawScene(); overlay((T.gameOver || "Game over") + "  " + lastScore, T.retry || "Tocca per riprovare"); }
      raf = requestAnimationFrame(loop);
    }
    loop();

    function onKey(e) { if (e.code === "Space") { e.preventDefault(); jump(); } }
    function onTap(e) {
      // tap in qualunque punto dello schermo = salto, tranne sui controlli interattivi
      if (e.button && e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest("input,select,textarea,button,a,label")) return;
      jump();
    }
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onTap);
    return { destroy() {
      cancelAnimationFrame(raf); ro.disconnect();
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onTap);
    } };
  };
  function Donut() {}
  window.Donut = window.Donut || Donut;
})();
