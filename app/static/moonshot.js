/* Moonshot — renderer condiviso della scena (razzo Terra→Luna), pixel-art a pixel piccoli.
   Puro disegno su canvas 3:4; lo stato arriva dal server via update(state). Usato da audience e presenter.
   API:  const r = Moonshot.mount(container, {texts});  r.update(state);  r.destroy(); */
(function () {
  const DIST_KM = 384400;

  function Moonshot() {}
  window.Moonshot = window.Moonshot || Moonshot;

  Moonshot.mount = function (container, opts) {
    opts = opts || {};
    const T = opts.texts || {};
    container.innerHTML = "";
    const cv = document.createElement("canvas");
    cv.style.width = "100%"; cv.style.display = "block"; cv.style.borderRadius = "10px";
    cv.style.imageRendering = "pixelated"; cv.style.background = "#05060f";
    container.appendChild(cv);
    const ctx = cv.getContext("2d");
    ctx.imageSmoothingEnabled = false;

    let W = 0, H = 0;
    function resize() {
      W = Math.max(280, Math.min(container.clientWidth, 900));
      H = Math.round(W * 4 / 3);
      cv.width = W; cv.height = H;
      ctx.imageSmoothingEnabled = false;
    }
    resize();
    const ro = new ResizeObserver(resize); ro.observe(container);

    // stelle su due strati (parallasse)
    function mkStars(n, spread) {
      const a = [];
      for (let i = 0; i < n; i++) a.push({ x: Math.random(), y: Math.random() * spread, r: Math.random() });
      return a;
    }
    const farStars = mkStars(140, 3), nearStars = mkStars(60, 3);

    let state = null;
    let frac = 0, fracTarget = 0;      // progresso interpolato (posizione razzo)
    let energy = 0, energyTarget = 0;  // room energy interpolata (gauge)
    let scroll = 0, frame = 0;
    let flash = 0, flashPower = 0, lastWindows = 0, lastResult = null;

    function update(s) {
      state = s;
      if (!s) return;
      if (s.progress_frac != null) fracTarget = s.progress_frac;
      if (s.room_energy != null) energyTarget = s.room_energy;
      // nuova finestra chiusa → fiammata + iperspazio, forza = ratio di quella finestra
      if (s.windows_used != null && s.windows_used > lastWindows) { flash = 1; flashPower = s.room_energy || 0; lastWindows = s.windows_used; }
      if (s.status !== "running") lastWindows = s.windows_used || 0;
      lastResult = s.result || null;
    }

    // --- pixel helpers ---
    function px(x, y, w, h, c) { ctx.fillStyle = c; ctx.fillRect(x | 0, y | 0, Math.ceil(w), Math.ceil(h)); }
    function lerp(a, b, t) { return a + (b - a) * t; }

    function drawStars(list, speed, size, colors, streak) {
      for (const st of list) {
        const x = st.x * W;
        const y = ((st.y / 3 * H + scroll * speed) % H + H) % H;
        const c = colors[(st.r * colors.length) | 0];
        if (streak > 1.5) {   // iperspazio: la stella diventa una scia verticale
          ctx.fillStyle = c;
          ctx.fillRect(x | 0, y | 0, Math.ceil(size), Math.ceil(size + streak * (0.4 + st.r)));
        } else px(x, y, size, size, c);
      }
    }

    function drawMoon(cx, cy, r) {
      // corpo con ombreggiatura + crateri (pixel)
      for (let yy = -r; yy <= r; yy++) for (let xx = -r; xx <= r; xx++) {
        if (xx * xx + yy * yy <= r * r) {
          const sh = (xx + yy) / (2 * r);
          const g = Math.max(0, Math.min(1, 0.55 + sh * 0.6));
          const v = (233 - g * 70) | 0;
          px(cx + xx, cy + yy, 1, 1, `rgb(${v},${v},${(v + 12)})`);
        }
      }
      const cr = [[-0.34, -0.12, 0.16], [0.22, 0.28, 0.2], [0.42, -0.3, 0.11], [-0.12, 0.4, 0.12]];
      for (const [ox, oy, rr] of cr) {
        const R = rr * r;
        for (let yy = -R; yy <= R; yy++) for (let xx = -R; xx <= R; xx++)
          if (xx * xx + yy * yy <= R * R) {
            const gx = cx + ox * r + xx, gy = cy + oy * r + yy;
            if ((gx - cx) ** 2 + (gy - cy) ** 2 <= r * r) px(gx, gy, 1, 1, "#9297ac");
          }
      }
    }

    function drawEarth(cx, cy, r) {
      const top = cy - r;
      for (let yy = Math.max(0, top); yy < H; yy++) for (let xx = 0; xx < W; xx++) {
        const dx = xx - cx, dy = yy - cy;
        if (dx * dx + dy * dy <= r * r) {
          const t = (yy - top) / (r);
          const b = 0x1f + (t * 0x50 | 0);
          px(xx, yy, 1, 1, `rgb(${(0x0f + t * 0x10) | 0},${(0x3c + t * 0x33) | 0},${(0x74 + t * 0x4c) | 0})`);
        }
      }
      // alone atmosfera
      ctx.strokeStyle = "rgba(90,180,240,.35)"; ctx.lineWidth = Math.max(2, W / 180);
      ctx.beginPath(); ctx.arc(cx, cy, r + ctx.lineWidth, Math.PI, 2 * Math.PI); ctx.stroke();
    }

    function drawRocket(cx, cy, u, air, boost) {
      const b = (gx, gy, gw, gh, c) => px(cx + gx * u, cy + gy * u, gw * u, gh * u, c);
      const red = "#e0503a", redd = "#b93a28", body = "#eef2fa", shade = "#b9c2d4",
        win = "#6fd8ec", wind = "#245a72", steel = "#8b93a8";
      b(6, 0, 2, 1, red); b(5, 1, 4, 1, red); b(5, 2, 4, 1, redd);      // nose
      b(4, 3, 6, 10, body); b(4, 3, 1, 10, shade); b(9, 3, 1, 10, shade); // body
      b(6, 5, 2, 2, win); b(6, 6, 2, 1, wind);                           // window
      b(4, 9, 6, 1, red);                                                // stripe
      b(2, 10, 2, 3, redd); b(10, 10, 2, 3, redd);                       // fins
      b(5, 13, 4, 1, steel); b(6, 14, 2, 1, steel);                      // nozzle
      // flame — flicker con air, fiammata con boost (si allarga e allunga)
      const f = air, e = boost | 0, wexp = e > 2 ? 1 : 0;
      b(6, 15, 2, 1, "#ffe066");
      b(5 - wexp, 16, 4 + 2 * wexp, 1 + f, "#ffc23a");
      b(5, 17 + f, 4, 1 + f + e, "#f0902a");
      b(6, 18 + f + e, 2, 1 + e, "#e0642a");
      b(7, 19 + f + 2 * e, 1, 1 + (e >> 1), "#c0431e");
    }

    function overlay(title, sub, color) {
      ctx.fillStyle = "rgba(6,8,18,.72)"; ctx.fillRect(0, 0, W, H);
      ctx.textAlign = "center"; ctx.fillStyle = color || "#eaf0fb";
      ctx.font = `900 ${Math.round(W / 11)}px system-ui,sans-serif`;
      ctx.fillText(title, W / 2, H / 2);
      if (sub) { ctx.fillStyle = "#c8d4f0"; ctx.font = `600 ${Math.round(W / 24)}px system-ui,sans-serif`; ctx.fillText(sub, W / 2, H / 2 + W / 12); }
    }

    function hud(s) {
      const cfg = s.config || {}, N = cfg.distance || 1, X = cfg.reserve || 0;
      const pad = Math.round(W * 0.03), fs = Math.round(W / 26);
      ctx.textAlign = "left";
      // altitudine
      ctx.fillStyle = "#8fa0c0"; ctx.font = `600 ${Math.round(fs * .7)}px system-ui`;
      ctx.fillText((T.altitude || "ALTITUDE"), pad, pad + fs * .7);
      ctx.fillStyle = "#eaf0fb"; ctx.font = `900 ${Math.round(fs * 1.5)}px system-ui,monospace`;
      const alt = (s.altitude_km || 0).toLocaleString() + " km";
      ctx.fillText(alt, pad, pad + fs * 2.3);
      // progress %
      ctx.fillStyle = "#16a3ab"; ctx.font = `800 ${fs}px system-ui`;
      ctx.fillText(Math.round((s.progress_frac || 0) * 100) + "%", pad, pad + fs * 3.6);
      // boost pips
      const used = s.windows_used || 0, py = pad + fs * 4.2, pw = Math.max(7, W / 40), gap = pw + 4;
      for (let i = 0; i < N + X; i++) {
        const isRes = i >= N;
        const on = i >= used;
        const c = on ? (isRes ? "#e8862e" : "#16a3ab") : (isRes ? "#2a2016" : "#22303f");
        px(pad + i * gap, py, pw, pw, c);
      }
      // room energy (destra)
      ctx.textAlign = "right"; ctx.fillStyle = "#8fa0c0"; ctx.font = `600 ${Math.round(fs * .7)}px system-ui`;
      ctx.fillText((T.energy || "ROOM ENERGY"), W - pad, pad + fs * .7);
      const gw = W * 0.28, gx = W - pad - gw, gy = pad + fs;
      px(gx, gy, gw, fs * .8, "#141a2c"); px(gx, gy, gw * energy, fs * .8, "#16a3ab");
      ctx.fillStyle = "#8fa0c0"; ctx.textAlign = "right";
      ctx.fillText((T.crew || "CREW") + " " + (s.total_crew || s.ready_count || 0), W - pad, gy + fs * 1.7);
    }

    let raf;
    function loop() {
      frame++;
      frac = lerp(frac, fracTarget, 0.08);
      energy = lerp(energy, energyTarget, 0.12);
      const boost = flash > 0 ? flash * flashPower : 0;   // 0..1 durante la fiammata
      scroll += 0.4 + frac * 2.2 + boost * 34;            // surge iperspazio al boost
      if (flash > 0) flash = Math.max(0, flash - 0.035);

      // sfondo gradiente
      const grad = ctx.createLinearGradient(0, 0, 0, H);
      grad.addColorStop(0, "#05060f"); grad.addColorStop(1, "#0c1a3a");
      ctx.fillStyle = grad; ctx.fillRect(0, 0, W, H);
      drawStars(farStars, 0.25, Math.max(1, W / 400), ["#26304e", "#2e3a5c", "#343f66"], boost * 26);
      drawStars(nearStars, 0.6, Math.max(2, W / 260), ["#c8d4f0", "#ffffff", "#9fb0e0"], boost * 62);

      // Luna (cresce parecchio col progresso) e Terra (si rimpicciolisce e si allontana)
      const moonR = W * (0.08 + frac * 0.15);
      drawMoon(W * 0.6, H * 0.15, moonR);
      const earthR = W * (0.95 - frac * 0.5);
      drawEarth(W / 2, H + earthR - W * (0.24 - frac * 0.22), earthR);

      // razzo: sale da in basso verso l'alto col progresso
      const u = Math.max(2, W / 90);
      const ry = lerp(H * 0.7, H * 0.24, frac);
      const air = (frame >> 2) & 1;
      drawRocket(W / 2 - 7 * u, ry, u, air, Math.round(boost * 7));

      if (boost > 0.12) { ctx.fillStyle = `rgba(255,225,110,${boost * .22})`; ctx.fillRect(0, 0, W, H); }

      const s = state;
      if (s && s.status === "running") {
        hud(s);
        if (s.window_open) {   // pulse "finestra aperta"
          const a = 0.35 + 0.35 * Math.abs(Math.sin(frame * 0.15));
          ctx.strokeStyle = `rgba(232,134,46,${a})`; ctx.lineWidth = Math.max(3, W / 90);
          ctx.strokeRect(ctx.lineWidth, ctx.lineWidth, W - 2 * ctx.lineWidth, H - 2 * ctx.lineWidth);
        }
        if (s.result === "success") overlay(T.reached || "MOON REACHED", null, "#7ee0a0");
        else if (s.result === "failed") overlay(T.incomplete || "MISSION INCOMPLETE",
          Math.round((s.progress_frac || 0) * 100) + "%", "#f0a0a0");
      } else if (s && s.status === "lobby") {
        overlay(T.moonshot || "MOONSHOT", (s.ready_count || 0) + " " + (T.ready_crew || "ready"), "#eaf0fb");
      }
      raf = requestAnimationFrame(loop);
    }
    loop();

    return { update, destroy() { cancelAnimationFrame(raf); ro.disconnect(); } };
  };
})();
