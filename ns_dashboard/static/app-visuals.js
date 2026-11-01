/* Nightshift Dashboard — visuals: deterministic posters (canvas → data URL), inline SVG charts,
   layout diagrams and small illustrations. No external assets; works offline and under the CSP
   (img-src allows data:, style-src allows inline styles). Exposed as window.NS.vis. */
(function () {
  "use strict";
  const NS = (window.NS = window.NS || {});
  const vis = (NS.vis = {});

  const hash = (s) => { let h = 2166136261; for (const c of String(s)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
  const PALETTES = [
    ["#1b2a41", "#3b5bdb", "#8fd3ff"], ["#2b1b3d", "#7b2cbf", "#f2b34c"], ["#102a2a", "#1f9d63", "#b7f0d2"],
    ["#3a1d1d", "#c44536", "#ffb86b"], ["#1c2541", "#0b7285", "#74c0fc"], ["#2d2a1b", "#b08900", "#ffe066"],
    ["#1f1b2e", "#5f3dc4", "#f783ac"], ["#0f2b3c", "#1971c2", "#a5d8ff"],
  ];
  vis.palette = (seed) => PALETTES[hash(seed) % PALETTES.length];

  const cache = new Map();
  /** Poster image for a title. kind: long|short|clip|story|channel. Returns a data URL (PNG). */
  vis.poster = (title, kind = "short", w = 540, h = 960, opts = {}) => {
    const key = `${title}|${kind}|${w}x${h}|${opts.label || ""}|${opts.noText ? "nt" : ""}`;
    if (cache.has(key)) return cache.get(key);
    const c = document.createElement("canvas"); c.width = w; c.height = h;
    const g = c.getContext("2d"); const [a, b, hi] = vis.palette(title);
    const grad = g.createLinearGradient(0, 0, w, h); grad.addColorStop(0, a); grad.addColorStop(0.62, b); grad.addColorStop(1, hi);
    g.fillStyle = grad; g.fillRect(0, 0, w, h);
    // soft blobs
    const n = 3 + (hash(title + "b") % 3);
    for (let i = 0; i < n; i++) { const r = Math.min(w, h) * (0.25 + ((hash(title + i) % 40) / 100)); g.beginPath(); g.arc((hash(title + "x" + i) % w), (hash(title + "y" + i) % h), r, 0, Math.PI * 2); g.fillStyle = `rgba(255,255,255,${0.05 + (i % 3) * 0.04})`; g.fill(); }
    // grain
    for (let i = 0; i < (w * h) / 900; i++) { g.fillStyle = `rgba(0,0,0,${Math.random() * 0.08})`; g.fillRect(Math.random() * w, Math.random() * h, 2, 2); }
    // bottom shade + title
    if (opts.noText) { const url0 = c.toDataURL("image/png"); cache.set(key, url0); return url0; }
    const shade = g.createLinearGradient(0, h * 0.45, 0, h); shade.addColorStop(0, "rgba(0,0,0,0)"); shade.addColorStop(1, "rgba(0,0,0,.72)"); g.fillStyle = shade; g.fillRect(0, 0, w, h);
    const pad = Math.round(w * 0.07); const size = Math.max(14, Math.round(Math.min(w, h) * (h > w ? 0.085 : 0.075)));
    g.fillStyle = "#fff"; g.font = `800 ${size}px system-ui, Segoe UI, Roboto, sans-serif`; g.textBaseline = "bottom";
    const words = String(title).split(" "); const lines = []; let line = "";
    for (const wd of words) { const t = line ? line + " " + wd : wd; if (g.measureText(t).width > w - pad * 2 && line) { lines.push(line); line = wd; } else line = t; if (lines.length === 3) break; }
    if (line && lines.length < 4) lines.push(line);
    const lh = size * 1.15; let y = h - pad;
    for (let i = lines.length - 1; i >= 0; i--) { g.fillText(lines[i], pad, y); y -= lh; }
    if (opts.label) { g.font = `700 ${Math.round(size * 0.55)}px system-ui, Segoe UI, Roboto, sans-serif`; g.fillStyle = "rgba(255,255,255,.85)"; g.fillText(opts.label.toUpperCase(), pad, y - size * 0.3); }
    const url = c.toDataURL("image/png"); cache.set(key, url); return url;
  };

  /** <img> tag string for a poster. */
  vis.posterImg = (title, kind, w, h, cls = "", label = "") => `<img class="${cls}" alt="" src="${vis.poster(title, kind, w, h, { label })}" width="${w}" height="${h}">`;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** Sparkline. values: number[] */
  vis.sparkline = (values, { w = 120, h = 36, color = "var(--accent-2)", fill = true } = {}) => {
    if (!values?.length) return "";
    const max = Math.max(...values), min = Math.min(...values); const span = max - min || 1;
    const pts = values.map((v, i) => [i * (w / (values.length - 1 || 1)), h - 3 - ((v - min) / span) * (h - 6)]);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const area = fill ? `<path d="${d} L${w},${h} L0,${h} Z" fill="${color}" opacity=".15"/>` : "";
    const last = pts.at(-1);
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">${area}<path d="${d}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}"/></svg>`;
  };

  /** Horizontal bars. items: [{label, value, color?, suffix?}] ; max defaults to 100. */
  vis.bars = (items, { w = 520, rowH = 40, max = 100, suffix = "%" } = {}) => {
    const labelW = Math.round(w * 0.46); const barW = w - labelW - 70;
    const rows = items.map((it, i) => { const y = i * rowH; const bw = Math.max(4, (Math.min(it.value, max) / max) * barW); const col = it.color || "var(--accent-2)";
      return `<text x="0" y="${y + rowH / 2 + 5}" font-size="14" font-weight="600" fill="var(--fg)">${esc(it.label.length > 34 ? it.label.slice(0, 33) + "…" : it.label)}</text>
        <rect x="${labelW}" y="${y + rowH / 2 - 9}" width="${barW}" height="18" rx="9" fill="var(--surface-2)"/>
        <rect x="${labelW}" y="${y + rowH / 2 - 9}" width="${bw.toFixed(1)}" height="18" rx="9" fill="${col}"/>
        <text x="${labelW + barW + 10}" y="${y + rowH / 2 + 5}" font-size="14" font-weight="700" fill="var(--fg)" class="num">${it.value}${suffix}</text>`; }).join("");
    return `<svg class="chart" viewBox="0 0 ${w} ${items.length * rowH}" width="100%" role="img" aria-label="bar chart">${rows}</svg>`;
  };

  /** Area chart with day labels. series: [{label, values:number[], color}], labels: string[] */
  vis.area = (series, labels, { w = 640, h = 220 } = {}) => {
    const padL = 44, padB = 28, padT = 12, padR = 12; const iw = w - padL - padR, ih = h - padT - padB;
    const all = series.flatMap((s) => s.values); const max = Math.max(1, ...all); const n = labels.length;
    const x = (i) => padL + (i * iw) / (n - 1 || 1); const y = (v) => padT + ih - (v / max) * ih;
    const grid = [0, 0.5, 1].map((f) => `<line x1="${padL}" x2="${w - padR}" y1="${(padT + ih - f * ih).toFixed(1)}" y2="${(padT + ih - f * ih).toFixed(1)}" stroke="var(--line)" stroke-dasharray="3 4"/><text x="${padL - 8}" y="${(padT + ih - f * ih + 4).toFixed(1)}" font-size="12" text-anchor="end" fill="var(--muted)" class="num">${Math.round(max * f).toLocaleString()}</text>`).join("");
    const paths = series.map((s) => { const d = s.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" "); return `<path d="${d} L${x(s.values.length - 1).toFixed(1)},${(padT + ih).toFixed(1)} L${padL},${(padT + ih).toFixed(1)} Z" fill="${s.color}" opacity=".14"/><path d="${d}" fill="none" stroke="${s.color}" stroke-width="2.5" stroke-linejoin="round"/>`; }).join("");
    const xl = labels.map((l, i) => (i % Math.ceil(n / 7) === 0 || i === n - 1) ? `<text x="${x(i).toFixed(1)}" y="${h - 8}" font-size="12" text-anchor="middle" fill="var(--muted)">${esc(l)}</text>` : "").join("");
    const legend = series.map((s, i) => `<g transform="translate(${padL + i * 140},${padT - 2})"><rect width="12" height="12" rx="3" fill="${s.color}"/><text x="18" y="11" font-size="12" fill="var(--muted)">${esc(s.label)}</text></g>`).join("");
    return `<svg class="chart" viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="trend chart">${grid}${paths}${xl}${legend}</svg>`;
  };

  /** Layout diagram for a format id, 9:16 or 16:9 box. */
  vis.layoutDiagram = (id, w = 72, h = 128) => {
    const R = (x, y, ww, hh, f, r = 6) => `<rect x="${x}" y="${y}" width="${ww}" height="${hh}" rx="${r}" fill="${f}"/>`;
    const A = "var(--accent-2)", B = "color-mix(in srgb, var(--warn) 70%, var(--surface))", G = "var(--surface-2)";
    let inner = R(0, 0, w, h, G, 10);
    const k = id.replace(/^clip_/, "");
    if (k === "gaming_facecam" || k === "screen_plus_cam") inner += R(6, 6, w - 12, h * 0.3, A) + R(6, h * 0.3 + 12, w - 12, h - h * 0.3 - 18, B);
    else if (k === "podcast_stacked") inner += R(6, 6, w - 12, h / 2 - 9, A) + R(6, h / 2 + 3, w - 12, h / 2 - 9, B);
    else if (k === "podcast_single" || k === "talking_head") inner += `<circle cx="${w / 2}" cy="${h * 0.42}" r="${w * 0.22}" fill="${A}"/>` + R(w * 0.25, h * 0.62, w * 0.5, h * 0.25, A, 8);
    else if (k === "center_crop") inner += R(w * 0.14, h * 0.2, w * 0.72, h * 0.6, A);
    else if (k === "long_16x9" || k === "long") inner += R(6, 6, w - 12, h - 12, A, 8);
    else inner += R(6, h * 0.1, w - 12, h * 0.8, A, 8);
    return `<svg class="layout" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">${inner}</svg>`;
  };

  /** Simple illustrations for the Create tiles. */
  vis.illus = (name) => {
    if (name === "story") return `<svg viewBox="0 0 120 80" width="120" height="80" aria-hidden="true"><rect x="8" y="10" width="72" height="60" rx="10" fill="var(--surface-2)"/><rect x="20" y="24" width="48" height="6" rx="3" fill="var(--accent-2)"/><rect x="20" y="36" width="40" height="6" rx="3" fill="var(--line)"/><rect x="20" y="48" width="44" height="6" rx="3" fill="var(--line)"/><rect x="86" y="18" width="26" height="46" rx="8" fill="var(--accent)"/><polygon points="95,33 95,49 108,41" fill="var(--accent-ink)"/></svg>`;
    if (name === "clips") return `<svg viewBox="0 0 120 80" width="120" height="80" aria-hidden="true"><rect x="6" y="14" width="64" height="40" rx="8" fill="var(--surface-2)"/><polygon points="32,26 32,42 44,34" fill="var(--accent-2)"/><rect x="78" y="6" width="16" height="30" rx="5" fill="var(--accent)"/><rect x="98" y="6" width="16" height="30" rx="5" fill="var(--accent)" opacity=".7"/><rect x="78" y="42" width="16" height="30" rx="5" fill="var(--accent)" opacity=".5"/><rect x="98" y="42" width="16" height="30" rx="5" fill="var(--accent)" opacity=".35"/></svg>`;
    return "";
  };

  /** Decorative waveform for a voice card. */
  vis.wave = (seed, w = 160, h = 36) => { const bars = []; const n = 28; for (let i = 0; i < n; i++) { const v = 6 + (hash(seed + i) % (h - 8)); bars.push(`<rect x="${(i * w) / n}" y="${(h - v) / 2}" width="${w / n - 2}" height="${v}" rx="2" fill="var(--accent-2)" opacity="${0.45 + (i % 3) * 0.2}"/>`); } return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">${bars.join("")}</svg>`; };

  /** Initial avatar */
  vis.avatar = (name, size = 44) => { const [a, b] = vis.palette(name); return `<span class="avatar" style="width:${size}px;height:${size}px;background:linear-gradient(135deg,${a},${b})">${esc(String(name).trim()[0] || "?").toUpperCase()}</span>`; };
})();
