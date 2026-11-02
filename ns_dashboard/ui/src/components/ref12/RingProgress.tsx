import { cx } from "../../ui/kit";
import { TONE_VAR, type Tone } from "./tokens";

/** RingProgress: 48 px ring, 4 px stroke, tint-2 track, tone arc starting at 12 o'clock with a round cap,
 *  percentage centred in 12 px semibold tabular figures. The arc animates stroke-dashoffset. */
export function RingProgress({ value, label, tone = "green", size = 48, stroke = 4, className }: {
  value: number; label: string; tone?: Tone; size?: 40 | 48 | 56 | 64; stroke?: 3 | 4 | 5; className?: string;
}) {
  const v = Math.max(0, Math.min(100, Math.round(value)));
  const r = (size - stroke) / 2; const c = 2 * Math.PI * r;
  return (
    <span role="img" aria-label={`${label}: ${v}%`} className={cx("relative inline-flex shrink-0 items-center justify-center", className)} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} style={{ stroke: "var(--r12-tint-2)" }} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={stroke} strokeLinecap="round" strokeDasharray={c}
          style={{ stroke: TONE_VAR[tone], strokeDashoffset: c * (1 - v / 100), transition: "stroke-dashoffset 600ms cubic-bezier(0.22, 1, 0.36, 1)" }} />
      </svg>
      <span aria-hidden className="absolute text-[12px] font-semibold text-r12-ink num">{v}%</span>
    </span>
  );
}
