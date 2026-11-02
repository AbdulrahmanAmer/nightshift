import { useId } from "react";
import { cx } from "../../ui/kit";
import { Card } from "./Card";

export interface MeterDatum {
  label: string;
  /** 0-100, or null when there is no reading (empty well, "–"). */
  value: number | null;
  /** Text above the well; defaults to the percentage. */
  display?: string;
  /** Where the reading comes from, read out with the label. */
  source?: string;
  tone?: "accent" | "highlight";
}

/** VerticalMeter (Planify Meters.tsx): an inset 12 x 112 well with a pill fill rising from the bottom (inset 3 px).
 *  The fill animates transform, not height. role="meter". */
export function VerticalMeter({ label, value, display, source, tone = "accent" }: MeterDatum) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value));
  const text = display ?? (value == null ? "–" : `${Math.round(pct)}%`);
  return (
    <div className="flex min-w-0 flex-col items-center gap-2">
      <span aria-hidden className="text-ds-xs font-medium tracking-[0.02em] ds-num">{text}</span>
      <span role="meter" aria-label={source ? `${label}, ${source}` : label} aria-valuenow={value == null ? undefined : Math.round(pct)} aria-valuetext={value == null ? (display ?? "No reading yet") : text} aria-valuemin={0} aria-valuemax={100}
        className="relative h-28 w-3 overflow-hidden rounded-full bg-ds-track shadow-[var(--ds-shadow-inset-sm),0_0_0_1px_var(--ds-border-subtle)]">
        {value != null && (
          <span className={cx("absolute inset-x-[3px] top-[3px] bottom-[3px] origin-bottom rounded-full transition-transform duration-500 ease-ds-out", tone === "accent" ? "bg-ds-accent" : "bg-ds-highlight")}
            style={{ transform: `scaleY(${Math.max(pct, 6) / 100})` }} />
        )}
      </span>
      <span aria-hidden className="max-w-full truncate text-ds-xs text-ds-text-2">{label}</span>
    </div>
  );
}

/** MeterGroup (Planify): a padding-md card of side-by-side VerticalMeters with an optional caption under them. */
export function MeterGroup({ title, meters, caption, className }: { title: string; meters: MeterDatum[]; caption?: string; className?: string }) {
  const id = useId();
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={cx("flex flex-col gap-4", className)}>
      <h2 id={id} className="text-ds-md leading-ds-snug font-semibold">{title}</h2>
      <div className="grid auto-cols-fr grid-flow-col gap-2">{meters.map((m) => <VerticalMeter key={m.label} {...m} />)}</div>
      {caption && <p className="text-ds-2xs leading-ds-snug text-ds-muted">{caption}</p>}
    </Card>
  );
}
