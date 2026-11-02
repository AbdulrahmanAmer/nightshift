import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

export interface SegmentItem<T extends string> { id: T; label: string; icon?: ReactNode; iconOnly?: boolean }

/** SegmentedToggle: the Light/Dark pill (variant "tray") and the Today-tasks view switch (variant "icons").
 *  tray: 36 px tall tint tray, 3 px inset, 30 px items with 8 px radius, active = accent fill.
 *  icons: bare 28 px icon buttons, active = 28 px accent circle. */
export function SegmentedToggle<T extends string>({ value, onChange, items, label, variant = "tray", className }: {
  value: T; onChange: (v: T) => void; items: SegmentItem<T>[]; label: string; variant?: "tray" | "icons"; className?: string;
}) {
  const tray = variant === "tray";
  return (
    <div role="radiogroup" aria-label={label} className={cx("inline-flex shrink-0 items-center", tray ? "h-9 gap-0.5 rounded-r12-control bg-r12-tint-2 p-[3px]" : "gap-1", className)}>
      {items.map((it) => {
        const on = it.id === value;
        return (
          <button key={it.id} type="button" role="radio" aria-checked={on} aria-label={it.iconOnly ? it.label : undefined} title={it.iconOnly ? it.label : undefined}
            onClick={() => onChange(it.id)}
            className={cx(
              "r12-focus inline-flex items-center justify-center gap-1.5 transition-[background-color,color] duration-200 ease-out",
              tray ? "h-[30px] rounded-[8px] px-2.5 text-[12px] font-medium" : "size-7 rounded-full",
              on ? "bg-r12-accent text-r12-on-accent" : tray ? "text-r12-text-2 hover:text-r12-ink" : "text-r12-muted hover:bg-r12-tint-2 hover:text-r12-ink",
            )}>
            {it.icon}{!it.iconOnly && it.label}
          </button>
        );
      })}
    </div>
  );
}
