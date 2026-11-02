import { cx } from "../../ui/kit";

export interface WeekDay { weekday: string; date: number; iso: string; label: string }

/** WeekStrip (Planify): 7 equal columns, 4 px gap. Weekday 2xs muted over a 30 px date circle (sm/600 tabular),
 *  8 px apart. Hover = accent-soft circle; selected = accent fill, 4 px accent-soft halo and the accent glow. */
export function WeekStrip({ days, selected, onSelect, label, className }: { days: WeekDay[]; selected: string; onSelect?: (iso: string) => void; label: string; className?: string }) {
  return (
    <div role="group" aria-label={label} className={cx("grid grid-cols-7 gap-1", className)}>
      {days.map((d) => {
        const on = d.iso === selected;
        return (
          <button key={d.iso} type="button" aria-pressed={on} aria-label={d.label} onClick={() => onSelect?.(d.iso)}
            className="ds-focus group flex flex-col items-center gap-2 rounded-ds-sm">
            <span className="text-ds-2xs text-ds-muted">{d.weekday}</span>
            <span className={cx("grid size-[30px] place-items-center rounded-full text-ds-sm font-semibold ds-num transition-colors duration-[120ms] ease-ds-out",
              on ? "bg-ds-accent text-ds-on-accent shadow-[0_0_0_4px_var(--ds-bg-accent-soft),var(--ds-shadow-accent)]" : "group-hover:bg-ds-accent-soft")}>{d.date}</span>
          </button>
        );
      })}
    </div>
  );
}
