import { useId } from "react";
import { cx } from "../../ui/kit";
import { Card } from "./Card";
import { pad, useNow } from "./useNow";

export interface DayChip<T extends string | number = string> { value: T; label: string; ariaLabel?: string }

/** DayChips (Planify Readouts.tsx): a row of round 32 px raised chips; selected ones take the selection colour.
 *  Each chip is a toggle (aria-pressed), so it serves both one-of and many-of pickers. */
export function DayChips<T extends string | number>({ days, selected, onToggle, label, className }: {
  days: readonly DayChip<T>[]; selected: readonly T[]; onToggle?: (value: T) => void; label: string; className?: string;
}) {
  return (
    <div role="group" aria-label={label} className={cx("flex flex-wrap gap-2", className)}>
      {days.map((d) => {
        const on = selected.includes(d.value);
        return (
          <button key={String(d.value)} type="button" aria-pressed={on} aria-label={d.ariaLabel} title={d.ariaLabel} onClick={() => onToggle?.(d.value)}
            className={cx("ds-focus grid size-8 place-items-center rounded-full text-ds-xs font-semibold ds-num transition-[background-color,box-shadow] duration-[120ms] ease-ds-out",
              on ? "bg-ds-selected text-ds-on-selected shadow-ds-selected" : "bg-ds-surface text-ds-text shadow-[var(--ds-shadow-control),0_0_0_1px_var(--ds-border-default)] hover:bg-ds-hover active:shadow-ds-inset-sm")}>
            {d.label}
          </button>
        );
      })}
    </div>
  );
}

/** TimeChip (Planify): an accent block holding a zero-padded number, its unit set small after it. */
export function TimeChip({ value, unit, digits = 2, className }: { value: number | string; unit?: string; digits?: number; className?: string }) {
  return (
    <span className={cx("inline-flex items-end gap-1", className)}>
      <span className="rounded-ds-md bg-ds-accent px-2 py-1 text-ds-xl leading-ds-tight tracking-[0.04em] text-ds-on-accent shadow-ds-accent ds-num">{typeof value === "number" ? pad(value, digits) : value}</span>
      {unit && <span className="pb-0.5 text-ds-xs font-semibold">{unit}</span>}
    </span>
  );
}

/** CountdownCard (Planify): days / hours / minutes left until `target`, refreshed every 30 s (role="timer").
 *  Without a target it shows `emptyText` instead of zeros. */
export function CountdownCard({ title, target, caption, emptyText, now: fixedNow, className }: { title: string; target: Date | null; caption?: string; emptyText: string; now?: Date; className?: string }) {
  const now = useNow(30_000, fixedNow);
  const id = useId();
  const total = target ? Math.max(0, Math.floor((target.getTime() - now.getTime()) / 60_000)) : 0;
  const d = Math.floor(total / 1440); const h = Math.floor((total % 1440) / 60); const m = total % 60;
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={cx("flex flex-col", className)}>
      <h2 id={id} className="text-ds-md leading-ds-snug font-semibold">{title}</h2>
      {target ? (
        <p role="timer" aria-label={`${d} days, ${h} hours, ${m} minutes left`} className="mt-3 flex flex-wrap gap-2">
          {d > 0 && <TimeChip value={d} unit="d" />}<TimeChip value={h} unit="h" /><TimeChip value={m} unit="m" />
        </p>
      ) : <p className="mt-3 text-ds-sm text-ds-text-2">{emptyText}</p>}
      {caption && <p className="mt-auto pt-3 text-ds-2xs text-ds-muted ds-num">{caption}</p>}
    </Card>
  );
}
