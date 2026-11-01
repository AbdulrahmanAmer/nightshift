/* Night scenes drawn in SVG, seeded by the title, so every story has its own still without any
   image files (works offline, CSP-safe). The subject follows the title's words: a house with a
   porch light, a row of shops, an apartment block, or an empty road with street lamps. Clips get a
   "studio" scene: a streamer's desk at night. */
import { useId, useMemo } from "react";

const hash = (s: string) => { let h = 2166136261; for (const c of s) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; };
function rng(seed: string) { let a = hash(seed) || 1; return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

const SKIES = [
  { top: "#081127", mid: "#18284a", low: "#3b4f73", far: "#121c33", near: "#0a1120", ground: "#060a14" },
  { top: "#06171c", mid: "#123238", low: "#2f5a5c", far: "#0d2328", near: "#071519", ground: "#040d10" },
  { top: "#100f22", mid: "#272442", low: "#57496a", far: "#1a1830", near: "#0e0d1c", ground: "#080711" },
  { top: "#0b121b", mid: "#1d2b3a", low: "#4a5a6a", far: "#141e2a", near: "#0b1119", ground: "#06090e" },
  { top: "#0a1224", mid: "#22304d", low: "#6b5866", far: "#151d31", near: "#0c1120", ground: "#070a13" },
];
const WARM = ["#ffc867", "#ffb547", "#ffd890", "#f6a13a"];

type Subject = "house" | "shops" | "flats" | "road";
const subjectFor = (t: string, r: () => number): Subject => {
  const s = t.toLowerCase();
  if (/door|porch|house|home|attic|basement|garden|neighbo|dog|nurse/.test(s)) return "house";
  if (/laundromat|bakery|shop|store|pizza|receipt|diner|cafe|dryer|market/.test(s)) return "shops";
  if (/hotel|floor|elevator|lift|apartment|flat|building|substitute|class/.test(s)) return "flats";
  if (/bus|road|stop|street|car|drive|highway|train/.test(s)) return "road";
  return (["house", "shops", "flats", "road"] as Subject[])[Math.floor(r() * 4)];
};

export function Scene({ seed, kind = "story", w = 1600, h = 900, className, title }: { seed: string; kind?: "story" | "clip"; w?: number; h?: number; className?: string; title?: string }) {
  const uid = useId().replace(/:/g, "");
  const svg = useMemo(() => (kind === "clip" ? studio(seed, w, h, uid) : night(seed, w, h, uid)), [seed, kind, w, h, uid]);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="xMidYMid slice" className={className} role="img" aria-label={title || "Scene from the video"}>
      {svg}
    </svg>
  );
}

function night(seed: string, W: number, H: number, id: string) {
  const r = rng(seed); const sky = SKIES[hash(seed + "sky") % SKIES.length]; const warm = WARM[hash(seed + "w") % WARM.length];
  const subject = subjectFor(seed, r);
  const portrait = H > W; const U = Math.min(W, H); const horizon = H * (portrait ? 0.68 : 0.7);
  const els: JSX.Element[] = [];
  // stars
  for (let i = 0; i < (portrait ? 60 : 80); i++) { const x = r() * W, y = r() * horizon * 0.75, s = 0.6 + r() * 1.6; els.push(<circle key={"s" + i} cx={x} cy={y} r={s * (U / 900)} fill="#e8eefc" opacity={0.25 + r() * 0.6} />); }
  // moon
  const mx = W * (portrait ? 0.68 : 0.2 + r() * 0.6), my = H * (portrait ? 0.16 : 0.18 + r() * 0.1), mr = U * 0.06; const phase = 0.35 + r() * 0.5;
  els.push(<circle key="halo" cx={mx} cy={my} r={mr * 4} fill={`url(#${id}h)`} />);
  els.push(<mask key="mm" id={`${id}mm`}><rect width={W} height={H} fill="#000" /><circle cx={mx} cy={my} r={mr} fill="#fff" /><circle cx={mx + mr * phase * 1.25} cy={my - mr * 0.25} r={mr * 0.95} fill="#000" /></mask>);
  els.push(<circle key="moon-dark" cx={mx} cy={my} r={mr} fill="#f4ecd6" opacity={0.06} />);
  els.push(<circle key="moon" cx={mx} cy={my} r={mr} fill="#f4ecd6" mask={`url(#${id}mm)`} />);
  // far skyline
  let x = -20;
  while (x < W + 20) {
    const bw = U * (0.05 + r() * 0.09), bh = H * (0.08 + r() * 0.16);
    els.push(<rect key={"f" + x} x={x} y={horizon - bh} width={bw} height={bh + 4} fill={sky.far} />);
    for (let k = 0; k < 3; k++) if (r() > 0.55) els.push(<rect key={`fw${x}-${k}`} x={x + bw * (0.2 + r() * 0.6)} y={horizon - bh * (0.2 + r() * 0.7)} width={U * 0.006} height={U * 0.008} fill={warm} opacity={0.5} />);
    x += bw + U * (0.004 + r() * 0.02);
  }
  // ground
  els.push(<rect key="ground" x={0} y={horizon} width={W} height={H - horizon} fill={sky.ground} />);
  els.push(<rect key="ground-edge" x={0} y={horizon} width={W} height={U * 0.004} fill={sky.near} />);

  const lamp = (lx: number, base: number, scale: number, key: string) => {
    const ph = U * 0.36 * scale, hw = U * 0.05 * scale;
    return (
      <g key={key}>
        <polygon points={`${lx},${base - ph + 6} ${lx - hw * 2.6},${base} ${lx + hw * 2.6},${base}`} fill={`url(#${id}c)`} />
        <ellipse cx={lx} cy={base} rx={hw * 2.8} ry={hw * 0.45} fill={warm} opacity={0.18} />
        <rect x={lx - U * 0.0035 * scale} y={base - ph} width={U * 0.007 * scale} height={ph} fill="#05070c" />
        <rect x={lx - hw * 0.45} y={base - ph - U * 0.012 * scale} width={hw * 0.9} height={U * 0.016 * scale} rx={U * 0.004} fill="#05070c" />
        <rect x={lx - hw * 0.3} y={base - ph + U * 0.003} width={hw * 0.6} height={U * 0.005 * scale} fill={warm} />
      </g>
    );
  };

  const cx = W * (portrait ? 0.5 : 0.56 + (r() - 0.5) * 0.16);
  if (subject === "house") {
    const hw = U * (portrait ? 0.62 : 0.42), hh = hw * 0.55, x0 = cx - hw / 2, base = horizon + U * 0.05;
    els.push(<rect key="hb" x={x0} y={base - hh} width={hw} height={hh} fill={sky.near} />);
    els.push(<polygon key="roof" points={`${x0 - hw * 0.06},${base - hh} ${cx},${base - hh - hw * 0.34} ${x0 + hw * 1.06},${base - hh}`} fill="#070b14" />);
    els.push(<rect key="chim" x={x0 + hw * 0.72} y={base - hh - hw * 0.3} width={hw * 0.06} height={hw * 0.14} fill="#070b14" />);
    const win = (wx: number, wy: number, ww: number, wh: number, lit: boolean, k: string) => <g key={k}><rect x={wx} y={wy} width={ww} height={wh} fill={lit ? warm : "#0f1626"} opacity={lit ? 0.92 : 1} /><rect x={wx + ww / 2 - 1.5} y={wy} width={3} height={wh} fill={sky.near} /><rect x={wx} y={wy + wh / 2 - 1.5} width={ww} height={3} fill={sky.near} /></g>;
    els.push(win(x0 + hw * 0.1, base - hh * 0.78, hw * 0.18, hh * 0.3, true, "w1"));
    els.push(win(x0 + hw * 0.72, base - hh * 0.78, hw * 0.18, hh * 0.3, r() > 0.5, "w2"));
    els.push(win(cx - hw * 0.08, base - hh - hw * 0.2, hw * 0.16, hw * 0.12, r() > 0.6, "w3"));
    // door + porch light
    els.push(<rect key="door" x={cx - hw * 0.07} y={base - hh * 0.62} width={hw * 0.14} height={hh * 0.62} fill="#04060b" />);
    els.push(<rect key="door-g" x={cx - hw * 0.07} y={base - hh * 0.62} width={hw * 0.14} height={hh * 0.62} fill={`url(#${id}d)`} />);
    els.push(<circle key="plg" cx={cx + hw * 0.12} cy={base - hh * 0.6} r={hw * 0.12} fill={`url(#${id}p)`} />);
    els.push(<circle key="pl" cx={cx + hw * 0.12} cy={base - hh * 0.6} r={hw * 0.012} fill="#fff3d6" />);
    els.push(<rect key="path" x={cx - hw * 0.07} y={base} width={hw * 0.14} height={H - base} fill="#0b111d" />);
    els.push(lamp(x0 - hw * 0.28, horizon + U * 0.1, portrait ? 0.9 : 1, "l1"));
  } else if (subject === "shops") {
    const n = portrait ? 2 : 3; const sw = U * (portrait ? 0.4 : 0.3), sh = sw * 0.85, base = horizon + U * 0.06; const lit = Math.floor(r() * n);
    const sx = cx - (n * sw) / 2;
    for (let i = 0; i < n; i++) {
      const x1 = sx + i * sw;
      els.push(<rect key={"sb" + i} x={x1} y={base - sh - (i % 2) * sw * 0.12} width={sw - 4} height={sh + (i % 2) * sw * 0.12} fill={sky.near} />);
      els.push(<rect key={"sw" + i} x={x1 + sw * 0.08} y={base - sh * 0.55} width={sw * 0.84 - 4} height={sh * 0.42} fill={i === lit ? warm : "#0e1524"} opacity={i === lit ? 0.9 : 1} />);
      if (i === lit) { els.push(<rect key="glow" x={x1 - sw * 0.1} y={base} width={sw * 1.2} height={U * 0.12} fill={`url(#${id}g)`} />); for (let k = 0; k < 3; k++) els.push(<rect key={"m" + k} x={x1 + sw * (0.16 + k * 0.24)} y={base - sh * 0.36} width={sw * 0.16} height={sh * 0.22} rx={sw * 0.08} fill="#2a1b05" opacity={0.55} />); }
      els.push(<rect key={"awn" + i} x={x1 + sw * 0.04} y={base - sh * 0.66} width={sw * 0.92 - 4} height={sh * 0.07} fill="#060910" />);
      els.push(<rect key={"up" + i} x={x1 + sw * 0.2} y={base - sh * 0.92} width={sw * 0.22} height={sh * 0.14} fill={r() > 0.6 ? warm : "#0e1524"} opacity={0.7} />);
    }
    els.push(lamp(sx + n * sw + U * 0.06, base + U * 0.04, 0.95, "l1"));
  } else if (subject === "flats") {
    const fw = U * (portrait ? 0.56 : 0.34), fh = H * (portrait ? 0.5 : 0.62), x0 = cx - fw / 2, base = horizon + U * 0.04;
    els.push(<rect key="fb" x={x0} y={base - fh} width={fw} height={fh} fill={sky.near} />);
    const cols = 5, rows = 9; const cw = fw / cols;
    for (let i = 0; i < cols; i++) for (let j = 0; j < rows; j++) { const on = r() > 0.82; els.push(<rect key={`fw${i}-${j}`} x={x0 + i * cw + cw * 0.22} y={base - fh + j * (fh / rows) + fh / rows * 0.25} width={cw * 0.56} height={fh / rows * 0.42} fill={on ? warm : "#0f1727"} opacity={on ? 0.85 : 1} />); }
    els.push(<rect key="roofline" x={x0 - fw * 0.03} y={base - fh - U * 0.012} width={fw * 1.06} height={U * 0.014} fill="#060910" />);
    els.push(lamp(x0 + fw + U * 0.12, horizon + U * 0.08, 0.9, "l1"));
    if (!portrait) els.push(lamp(x0 - U * 0.16, horizon + U * 0.1, 1, "l2"));
  } else {
    const vx = cx; // vanishing road
    els.push(<polygon key="road" points={`${vx - U * 0.02},${horizon} ${vx + U * 0.02},${horizon} ${W * (portrait ? 0.95 : 0.8)},${H} ${W * (portrait ? 0.05 : 0.2)},${H}`} fill="#0b111c" />);
    for (let i = 0; i < 6; i++) { const t = (i + 0.5) / 6, y = horizon + (H - horizon) * t * t, ww = U * 0.004 + t * U * 0.012; els.push(<rect key={"dash" + i} x={vx - ww / 2} y={y} width={ww} height={U * 0.01 + t * U * 0.04} fill="#d9d1b8" opacity={0.35} />); }
    for (let i = 0; i < 3; i++) { const t = 0.35 + i * 0.3; els.push(lamp(vx - (W * 0.36) * t, horizon + (H - horizon) * t * t * 0.9, 0.35 + t * 0.75, "ll" + i)); }
    els.push(<g key="stop"><rect x={vx + W * 0.2} y={horizon + U * 0.02} width={U * 0.16} height={U * 0.1} fill="#0a0f1a" /><rect x={vx + W * 0.2 + U * 0.01} y={horizon + U * 0.03} width={U * 0.14} height={U * 0.05} fill={warm} opacity={0.35} /></g>);
  }
  return (
    <>
      <defs>
        <linearGradient id={`${id}s`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={sky.top} /><stop offset="0.55" stopColor={sky.mid} /><stop offset="1" stopColor={sky.low} /></linearGradient>
        <radialGradient id={`${id}h`}><stop offset="0" stopColor="#f4ecd6" stopOpacity="0.22" /><stop offset="1" stopColor="#f4ecd6" stopOpacity="0" /></radialGradient>
        <radialGradient id={`${id}p`}><stop offset="0" stopColor={warm} stopOpacity="0.55" /><stop offset="1" stopColor={warm} stopOpacity="0" /></radialGradient>
        <linearGradient id={`${id}c`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={warm} stopOpacity="0.32" /><stop offset="1" stopColor={warm} stopOpacity="0.02" /></linearGradient>
        <linearGradient id={`${id}g`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={warm} stopOpacity="0.28" /><stop offset="1" stopColor={warm} stopOpacity="0" /></linearGradient>
        <linearGradient id={`${id}d`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={warm} stopOpacity="0.05" /><stop offset="1" stopColor={warm} stopOpacity="0.25" /></linearGradient>
        <linearGradient id={`${id}f`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#c9d4e6" stopOpacity="0" /><stop offset="1" stopColor="#c9d4e6" stopOpacity="0.1" /></linearGradient>
        <radialGradient id={`${id}v`} cx="0.5" cy="0.45" r="0.75"><stop offset="0.55" stopColor="#000" stopOpacity="0" /><stop offset="1" stopColor="#000" stopOpacity="0.55" /></radialGradient>
      </defs>
      <rect width={W} height={H} fill={`url(#${id}s)`} />
      {els}
      <rect x={0} y={horizon - H * 0.12} width={W} height={H * 0.2} fill={`url(#${id}f)`} />
      <rect width={W} height={H} fill={`url(#${id}v)`} />
    </>
  );
}

function studio(seed: string, W: number, H: number, id: string) {
  const r = rng(seed); const tints = ["#3fb5a8", "#f2b33d", "#d0607a", "#6c8cff"]; const led = tints[hash(seed + "led") % tints.length];
  const U = Math.min(W, H); const cx = W / 2; const desk = H * 0.74;
  const els: JSX.Element[] = [];
  // shelves with small objects
  for (let i = 0; i < 2; i++) { const y = H * (0.2 + i * 0.16); els.push(<rect key={"sh" + i} x={W * 0.06} y={y} width={W * 0.26} height={U * 0.012} fill="#1d2230" />); for (let k = 0; k < 4; k++) { const bw = U * (0.02 + r() * 0.03), bh = U * (0.04 + r() * 0.06); els.push(<rect key={`b${i}${k}`} x={W * 0.07 + k * W * 0.06} y={y - bh} width={bw} height={bh} fill={["#2a3142", "#353b4f", "#232838"][k % 3]} />); } }
  // LED strip
  els.push(<rect key="led" x={W * 0.6} y={H * 0.08} width={W * 0.34} height={U * 0.008} fill={led} />);
  els.push(<rect key="ledg" x={W * 0.52} y={H * 0.08} width={W * 0.5} height={H * 0.4} fill={`url(#${id}l)`} />);
  // monitor
  const mw = W * 0.62, mh = mw * 0.5, mx = cx - mw / 2, my = desk - mh - U * 0.08;
  els.push(<rect key="mglow" x={mx - mw * 0.2} y={my - mh * 0.2} width={mw * 1.4} height={mh * 1.6} fill={`url(#${id}m)`} />);
  els.push(<rect key="mon" x={mx} y={my} width={mw} height={mh} rx={U * 0.01} fill="#0b0e15" />);
  els.push(<rect key="scr" x={mx + U * 0.01} y={my + U * 0.01} width={mw - U * 0.02} height={mh - U * 0.02} fill={`url(#${id}s)`} />);
  for (let i = 0; i < 4; i++) els.push(<rect key={"ui" + i} x={mx + mw * 0.06} y={my + mh * (0.15 + i * 0.12)} width={mw * (0.2 + r() * 0.3)} height={mh * 0.05} rx={2} fill="#ffffff" opacity={0.18} />);
  els.push(<rect key="stand" x={cx - U * 0.015} y={my + mh} width={U * 0.03} height={U * 0.08} fill="#0b0e15" />);
  // desk
  els.push(<rect key="desk" x={0} y={desk} width={W} height={H - desk} fill="#0c0f16" />);
  els.push(<rect key="desk-e" x={0} y={desk} width={W} height={U * 0.01} fill="#1b2130" />);
  // person silhouette (back to camera, lit by screen)
  const px = cx + (r() - 0.5) * W * 0.12, ph = H * 0.34;
  els.push(<ellipse key="head" cx={px} cy={desk - ph * 0.92} rx={U * 0.075} ry={U * 0.09} fill="#05070b" />);
  els.push(<path key="body" d={`M ${px - U * 0.26} ${H} C ${px - U * 0.26} ${desk - ph * 0.45}, ${px - U * 0.12} ${desk - ph * 0.62}, ${px} ${desk - ph * 0.64} C ${px + U * 0.12} ${desk - ph * 0.62}, ${px + U * 0.26} ${desk - ph * 0.45}, ${px + U * 0.26} ${H} Z`} fill="#05070b" />);
  els.push(<path key="rim" d={`M ${px - U * 0.07} ${desk - ph * 0.98} A ${U * 0.075} ${U * 0.09} 0 0 1 ${px + U * 0.07} ${desk - ph * 0.98}`} stroke={led} strokeOpacity={0.45} strokeWidth={U * 0.006} fill="none" />);
  // headphones band + mic arm
  els.push(<path key="hp" d={`M ${px - U * 0.08} ${desk - ph * 0.9} A ${U * 0.085} ${U * 0.1} 0 0 1 ${px + U * 0.08} ${desk - ph * 0.9}`} stroke="#1a1f2b" strokeWidth={U * 0.016} fill="none" />);
  els.push(<path key="arm" d={`M ${W * 0.92} ${desk} L ${W * 0.8} ${desk - ph * 0.7} L ${px + U * 0.16} ${desk - ph * 0.62}`} stroke="#141925" strokeWidth={U * 0.012} fill="none" />);
  els.push(<rect key="mic" x={px + U * 0.12} y={desk - ph * 0.7} width={U * 0.05} height={U * 0.09} rx={U * 0.025} fill="#1e2433" />);
  return (
    <>
      <defs>
        <linearGradient id={`${id}b`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#10131b" /><stop offset="1" stopColor="#1a1f2b" /></linearGradient>
        <linearGradient id={`${id}l`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={led} stopOpacity="0.28" /><stop offset="1" stopColor={led} stopOpacity="0" /></linearGradient>
        <radialGradient id={`${id}m`}><stop offset="0" stopColor="#9fc2ff" stopOpacity="0.22" /><stop offset="1" stopColor="#9fc2ff" stopOpacity="0" /></radialGradient>
        <linearGradient id={`${id}s`} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#24406b" /><stop offset="1" stopColor="#14223d" /></linearGradient>
        <radialGradient id={`${id}v`} cx="0.5" cy="0.45" r="0.75"><stop offset="0.55" stopColor="#000" stopOpacity="0" /><stop offset="1" stopColor="#000" stopOpacity="0.5" /></radialGradient>
      </defs>
      <rect width={W} height={H} fill={`url(#${id}b)`} />
      {els}
      <rect width={W} height={H} fill={`url(#${id}v)`} />
    </>
  );
}
