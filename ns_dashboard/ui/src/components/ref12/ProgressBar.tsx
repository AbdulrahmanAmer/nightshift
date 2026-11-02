import { cx } from "../../ui/kit";
import type { Tone } from "./Badge";

export const PROGRESS_FILL: Record<Tone, string> = { accent: "bg-ds-accent", success: "bg-ds-success", danger: "bg-ds-danger", neutral: "bg-ds-muted" };

/** ProgressBar (Planify API): "90%" in xs/600 tabular, then a 4 px pill track (chart-track) at least 48 px wide.
 *  The fill animates transform (scaleX), not width. */
export function ProgressBar({ value, tone = "accent", showValue = true, label, maxTrack, className }: { value: number; tone?: Tone; showValue?: boolean; label: string; maxTrack?: number; className?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <div className={cx("flex items-center gap-3", className)}>
      {showValue && <span className="min-w-[3ch] text-ds-xs font-semibold ds-num">{pct}%</span>}
      <div role="progressbar" aria-label={label} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
        className="relative h-1 min-w-12 flex-1 overflow-hidden rounded-full bg-ds-track" style={maxTrack ? { maxWidth: maxTrack } : undefined}>
        <span className={cx("absolute inset-0 origin-left rounded-full transition-transform duration-200 ease-ds-out", PROGRESS_FILL[tone])} style={{ transform: `scaleX(${pct / 100})` }} />
      </div>
    </div>
  );
}
