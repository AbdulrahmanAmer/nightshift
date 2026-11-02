import { cx } from "../../ui/kit";
import { TONE_BG, type Tone } from "./tokens";

/** ProgressBar: "90% ▬▬▬" — 12 px semibold tabular percentage, then a 4 px track (tint-2) with a tone fill.
 *  Track width defaults to 72 px; the fill animates its transform, never its width. */
export function ProgressBar({ value, label, tone = "accent", width = 72, showValue = true, className }: {
  value: number; label: string; tone?: Tone; width?: number | "full"; showValue?: boolean; className?: string;
}) {
  const v = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <div className={cx("flex items-center gap-2", width === "full" && "w-full", className)}>
      {showValue && <span className="w-9 shrink-0 text-right text-[12px] leading-4 font-semibold text-r12-ink num">{v}%</span>}
      <div role="progressbar" aria-label={label} aria-valuenow={v} aria-valuemin={0} aria-valuemax={100}
        className={cx("h-1 overflow-hidden rounded-full bg-r12-tint-2", width === "full" && "flex-1")} style={width === "full" ? undefined : { width }}>
        <div className={cx("h-full w-full origin-left rounded-full transition-transform duration-500 ease-[var(--ease-out)]", TONE_BG[tone])} style={{ transform: `scaleX(${v / 100})` }} />
      </div>
    </div>
  );
}
