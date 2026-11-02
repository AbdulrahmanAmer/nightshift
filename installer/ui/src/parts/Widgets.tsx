// Installer-local widgets that the shared ref12 set does not have yet (the planify "installer widgets" branch never landed):
// NeedMeters (VerticalMeter with a danger tone and a caption), TimeLeftCard (CountdownCard with units), DriveStepper
// (IntakeStepper with readOnly and the drive vessel). They read only --ds-* tokens, and move transform, not size.
import { useId, type ReactNode } from "react";
import { Card, TimeChip } from "@ref12";
import { cx } from "@dash/ui/kit";

/* ------------------------------------------------------------------ meters */

export interface NeedMeter { label: string; value: number; tone: "accent" | "highlight" | "danger"; caption: string }

const FILL = { accent: "bg-ds-accent", highlight: "bg-ds-highlight", danger: "bg-ds-danger" } as const;

export function NeedMeters({ title, meters, className }: { title: string; meters: NeedMeter[]; className?: string }) {
  const id = useId();
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={cx("flex flex-col gap-4", className)}>
      <h2 id={id} className="text-ds-md leading-ds-snug font-semibold">{title}</h2>
      <div className="grid grid-cols-3 gap-2">
        {meters.map((m) => {
          const pct = Math.max(0, Math.min(100, m.value));
          return (
            <div key={m.label} className="flex min-w-0 flex-col items-center gap-2">
              <span aria-hidden className="text-ds-xs font-medium ds-num">{Math.round(pct)}%</span>
              <span role="meter" aria-label={`${m.label}, ${m.caption}`} aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-valuetext={`${Math.round(pct)} percent, ${m.caption}`}
                className="relative h-24 w-3 overflow-hidden rounded-full bg-ds-track shadow-[var(--ds-shadow-inset-sm),0_0_0_1px_var(--ds-border-subtle)]">
                <span className={cx("absolute inset-x-[3px] top-[3px] bottom-[3px] origin-bottom rounded-full transition-transform duration-500 ease-ds-out", FILL[m.tone])} style={{ transform: `scaleY(${Math.max(pct, 6) / 100})` }} />
              </span>
              <span aria-hidden className="max-w-full truncate text-ds-xs font-medium text-ds-text-2">{m.label}</span>
              <span aria-hidden className="max-w-full truncate text-ds-2xs text-ds-muted ds-num">{m.caption}</span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ time left */

/** Hours and minutes left as two TimeChips. `etaS` null shows `emptyText` instead (measuring, paused). */
export function TimeLeftCard({ title, etaS, emptyText, children, className }: { title: string; etaS: number | null; emptyText: string; children?: ReactNode; className?: string }) {
  const id = useId();
  const total = etaS == null ? 0 : Math.ceil(etaS / 60);
  const h = Math.floor(total / 60); const m = total % 60;
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={cx("flex flex-col", className)}>
      <h2 id={id} className="text-ds-md leading-ds-snug font-semibold">{title}</h2>
      {etaS != null ? (
        <p role="timer" aria-label={`${h} hours, ${m} minutes left`} className="mt-3 flex flex-wrap gap-2"><TimeChip value={h} unit="h" /><TimeChip value={m} unit="m" /></p>
      ) : <p className="mt-3 text-ds-sm text-ds-text-2">{emptyText}</p>}
      {children && <div className="mt-auto flex flex-col gap-0.5 pt-4 text-ds-sm ds-num">{children}</div>}
    </Card>
  );
}

/* ------------------------------------------------------------------ drive stepper */

const DRIVE = "M8 14h48a4 4 0 0 1 4 4v28a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V18a4 4 0 0 1 4-4Z";

/** A read-only stepper: a drive bay that fills as steps finish, with "n/max" under it. */
export function DriveStepper({ title, value, max, unit, className }: { title: string; value: number; max: number; unit: string; className?: string }) {
  const id = useId(); const clip = useId();
  const level = max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={cx("flex flex-col", className)}>
      <h2 id={id} className="text-ds-md leading-ds-snug font-semibold">{title}</h2>
      <div className="flex flex-1 flex-col items-center justify-center gap-3 py-2">
        <svg viewBox="0 0 64 64" role="img" aria-label={`${value} of ${max} ${unit}s done`} className="w-36">
          <defs><clipPath id={clip}><path d={DRIVE} /></clipPath></defs>
          <path d={DRIVE} fill="var(--ds-bg-sunken)" />
          <g clipPath={`url(#${clip})`}>
            <rect x="4" y="14" width="56" height="36" fill="var(--ds-bg-accent)" style={{ transformBox: "fill-box", transformOrigin: "bottom", transform: `scaleY(${level})`, transition: "transform 500ms var(--ds-ease-out)" }} />
          </g>
          <path d={DRIVE} fill="none" stroke="var(--ds-text-muted)" strokeWidth="1.5" />
          <circle cx="48" cy="42" r="1.5" fill="var(--ds-text-muted)" /><circle cx="54" cy="42" r="1.5" fill="var(--ds-text-muted)" />
        </svg>
        <output className="text-ds-xl leading-ds-tight font-semibold ds-num">{value}/{max}</output>
      </div>
    </Card>
  );
}
