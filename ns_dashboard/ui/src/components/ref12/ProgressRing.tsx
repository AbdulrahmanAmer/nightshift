import { cx } from "../../ui/kit";
import type { Tone } from "./Badge";

const STROKE: Record<Tone, string> = { accent: "var(--ds-bg-accent)", success: "var(--ds-bg-success)", danger: "var(--ds-text-danger)", neutral: "var(--ds-text-muted)" };

/** ProgressRing (Planify): circular progress, arc from 12 o'clock with a round cap, percentage centred in sm/700 tabular.
 *  Default 56 px / 4 px; stat cards use 48. The arc animates stroke-dashoffset (600 ms). */
export function ProgressRing({ value, tone = "accent", size = 56, thickness = 4, label, className }: { value: number; tone?: Tone; size?: number; thickness?: number; label: string; className?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  const r = (size - thickness) / 2; const c = 2 * Math.PI * r; const m = size / 2;
  return (
    <div role="progressbar" aria-label={label} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
      className={cx("relative inline-grid shrink-0 place-items-center", className)} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden className="absolute inset-0">
        <circle cx={m} cy={m} r={r} fill="none" strokeWidth={thickness} style={{ stroke: "var(--ds-chart-track)" }} />
        <circle cx={m} cy={m} r={r} fill="none" strokeWidth={thickness} strokeLinecap="round" strokeDasharray={c} transform={`rotate(-90 ${m} ${m})`}
          style={{ stroke: STROKE[tone], strokeDashoffset: c * (1 - pct / 100), transition: "stroke-dashoffset 600ms var(--ds-ease-out)" }} />
      </svg>
      <span aria-hidden className="text-ds-sm font-bold ds-num">{pct}%</span>
    </div>
  );
}
