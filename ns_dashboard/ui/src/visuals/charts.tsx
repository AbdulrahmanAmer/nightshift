import { useId, useState } from "react";
import { fmtCompact, fmtNum } from "../lib/format";

const smooth = (pts: [number, number][]) => {
  if (pts.length < 2) return "";
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1] || pts[i]; const [x1, y1] = pts[i]; const [x2, y2] = pts[i + 1]; const [x3, y3] = pts[i + 2] || pts[i + 1];
    d += ` C ${x1 + (x2 - x0) / 6} ${y1 + (y2 - y0) / 6}, ${x2 - (x3 - x1) / 6} ${y2 - (y3 - y1) / 6}, ${x2} ${y2}`;
  }
  return d;
};

/** Main Results chart: Shorts (accent, leading) against full videos (ink), smooth lines, muted grid. */
export function ViewsChart({ days, height = 300 }: { days: { date: string; shorts: number; long: number }[]; height?: number }) {
  const id = useId().replace(/:/g, ""); const [hover, setHover] = useState<number | null>(null);
  const W = 900, H = height, pl = 44, pr = 28, pt = 16, pb = 28; const iw = W - pl - pr, ih = H - pt - pb;
  const max = Math.max(...days.map((d) => d.shorts), ...days.map((d) => d.long), 1);
  const mag = 10 ** Math.floor(Math.log10(max)); const step = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => max / s <= 6) || mag; const nice = Math.ceil(max / step) * step; const ticks = Array.from({ length: Math.round(nice / step) + 1 }, (_, i) => i * step);
  const X = (i: number) => pl + (i / (days.length - 1)) * iw; const Y = (v: number) => pt + ih - (v / nice) * ih;
  const sp = days.map((d, i) => [X(i), Y(d.shorts)] as [number, number]); const lp = days.map((d, i) => [X(i), Y(d.long)] as [number, number]);
  const label = (iso: string) => { const d = new Date(iso + "T12:00:00"); return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }); };
  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label="Views per day over the last 14 days, Shorts and full videos"
        onMouseLeave={() => setHover(null)} onMouseMove={(e) => { const b = (e.currentTarget as SVGSVGElement).getBoundingClientRect(); const x = ((e.clientX - b.left) / b.width) * W; setHover(Math.max(0, Math.min(days.length - 1, Math.round(((x - pl) / iw) * (days.length - 1))))); }}>
        <defs><linearGradient id={`${id}a`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="var(--accent)" stopOpacity="0.28" /><stop offset="1" stopColor="var(--accent)" stopOpacity="0" /></linearGradient></defs>
        {ticks.map((t) => <g key={t}><line x1={pl} x2={W - pr} y1={Y(t)} y2={Y(t)} stroke="var(--line)" /><text x={pl - 10} y={Y(t) + 4} textAnchor="end" fontSize="11" fill="var(--muted)" fontFamily="var(--font-mono)">{fmtCompact(t)}</text></g>)}
        {days.map((d, i) => (i % 2 === 1 || i === days.length - 1) && i !== days.length - 2 ? <text key={d.date} x={X(i)} y={H - 8} textAnchor={i === days.length - 1 ? "end" : "middle"} fontSize="11" fill="var(--muted)" fontFamily="var(--font-mono)">{label(d.date)}</text> : null)}
        <path d={`${smooth(sp)} L ${X(days.length - 1)} ${pt + ih} L ${pl} ${pt + ih} Z`} fill={`url(#${id}a)`} />
        <path d={smooth(sp)} fill="none" stroke="var(--accent)" strokeWidth={2.5} strokeLinecap="round" />
        <path d={smooth(lp)} fill="none" stroke="var(--text)" strokeWidth={1.5} strokeLinecap="round" />
        {hover != null && <g><line x1={X(hover)} x2={X(hover)} y1={pt} y2={pt + ih} stroke="var(--line-2)" /><circle cx={X(hover)} cy={Y(days[hover].shorts)} r={4.5} fill="var(--accent)" stroke="var(--card)" strokeWidth={1.5} /><circle cx={X(hover)} cy={Y(days[hover].long)} r={4} fill="var(--text)" stroke="var(--card)" strokeWidth={1.5} /></g>}
      </svg>
      {hover != null && (
        <div className="pointer-events-none absolute top-2 rounded-[12px] border border-line bg-card px-3 py-2 text-[12px] shadow-float" style={{ left: `clamp(8px, calc(${(X(hover) / W) * 100}% - 70px), calc(100% - 150px))` }}>
          <div className="font-mono text-muted">{label(days[hover].date)}</div>
          <div className="mt-1 flex items-center gap-2"><span className="size-2 rounded-full bg-accent" />Shorts <span className="ml-auto font-mono num">{fmtNum(days[hover].shorts)}</span></div>
          <div className="flex items-center gap-2"><span className="size-2 rounded-full bg-text" />Full videos <span className="ml-auto pl-3 font-mono num">{fmtNum(days[hover].long)}</span></div>
        </div>
      )}
    </div>
  );
}

/** Small layout diagram for a clip template or video format. */
export function LayoutDiagram({ id, w = 54, h = 96 }: { id: string; w?: number; h?: number }) {
  const key = id.replace(/^clip_/, "");
  const r = (x: number, y: number, ww: number, hh: number, tone: "a" | "b" | "c", k: string) => <rect key={k} x={x * w} y={y * h} width={ww * w} height={hh * h} rx={3} fill={tone === "a" ? "var(--text-2)" : tone === "b" ? "var(--line-2)" : "var(--accent)"} opacity={tone === "a" ? 0.55 : 1} />;
  const parts: Record<string, JSX.Element[]> = {
    auto: [r(0.12, 0.3, 0.76, 0.4, "a", "a"), r(0.3, 0.12, 0.4, 0.08, "c", "c")],
    gaming_facecam: [r(0.08, 0.06, 0.84, 0.3, "c", "cam"), r(0.08, 0.4, 0.84, 0.54, "a", "g")],
    podcast_stacked: [r(0.08, 0.06, 0.84, 0.42, "c", "a"), r(0.08, 0.52, 0.84, 0.42, "a", "b")],
    podcast_single: [r(0.08, 0.06, 0.84, 0.88, "a", "a"), r(0.3, 0.22, 0.4, 0.22, "c", "f")],
    talking_head: [r(0.08, 0.06, 0.84, 0.88, "a", "a"), r(0.32, 0.16, 0.36, 0.22, "c", "f")],
    center_crop: [r(0, 0, 1, 1, "b", "blur"), r(0.08, 0.28, 0.84, 0.44, "a", "m")],
    screen_plus_cam: [r(0.3, 0.05, 0.4, 0.2, "c", "cam"), r(0.06, 0.3, 0.88, 0.64, "a", "s")],
    long_16x9: [r(0.04, 0.06, 0.92, 0.88, "a", "a"), r(0.1, 0.72, 0.5, 0.1, "c", "cap")],
    short_9x16: [r(0.08, 0.06, 0.84, 0.88, "a", "a"), r(0.16, 0.6, 0.68, 0.08, "c", "cap")],
  };
  const body = parts[key] || parts.auto;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden className="block shrink-0">
      <rect x={0.5} y={0.5} width={w - 1} height={h - 1} rx={6} fill="var(--sunken)" stroke="var(--line-2)" />
      {body}
    </svg>
  );
}

export function Waveform({ seed, bars = 40, active }: { seed: string; bars?: number; active?: boolean }) {
  let a = 0; for (const c of seed) a = (a * 31 + c.charCodeAt(0)) >>> 0;
  const vals = Array.from({ length: bars }, (_, i) => { a = (a * 1103515245 + 12345) >>> 0; const env = Math.sin((i / bars) * Math.PI) * 0.6 + 0.4; return 0.15 + ((a % 1000) / 1000) * 0.85 * env; });
  return (
    <svg viewBox={`0 0 ${bars * 6} 32`} className="block h-8 w-full" aria-hidden preserveAspectRatio="none">
      {vals.map((v, i) => <rect key={i} x={i * 6 + 1} y={16 - v * 14} width={3} height={v * 28} rx={1.5} fill={active ? "var(--accent)" : "var(--text-2)"} opacity={active ? 1 : 0.45} />)}
    </svg>
  );
}
