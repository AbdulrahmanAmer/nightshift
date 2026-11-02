import { useRef, type KeyboardEvent } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";

export interface SegmentOption<T extends string> { value: T; label?: string; icon?: IconName; ariaLabel?: string }

/** SegmentedControl (Planify): radio group with roving focus and arrow keys.
 *  filled = tinted track (Light/Dark switch); plain = no track, round active item (list/grid/board).
 *  md items 30 px, sm 26 px; active = accent fill with the accent glow. */
export function SegmentedControl<T extends string>({ options, value, onChange, variant = "filled", size = "md", label, className }: {
  options: readonly SegmentOption<T>[]; value: T; onChange: (v: T) => void; variant?: "filled" | "plain"; size?: "sm" | "md"; label: string; className?: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0; if (!step) return;
    e.preventDefault(); const n = (i + step + options.length) % options.length; onChange(options[n]!.value); refs.current[n]?.focus();
  };
  return (
    <div role="radiogroup" aria-label={label} className={cx("inline-flex shrink-0 items-center gap-0.5 rounded-ds-md", variant === "filled" && "bg-ds-accent-soft p-[3px]", className)}>
      {options.map((o, i) => {
        const on = o.value === value; const iconOnly = !o.label;
        return (
          <button key={o.value} ref={(el) => { refs.current[i] = el; }} type="button" role="radio" aria-checked={on} aria-label={iconOnly ? o.ariaLabel : undefined} title={iconOnly ? o.ariaLabel : undefined}
            tabIndex={on ? 0 : -1} onClick={() => onChange(o.value)} onKeyDown={(e) => onKey(e, i)}
            className={cx(
              "ds-focus inline-flex items-center justify-center gap-1.5 font-medium transition-[background-color,color] duration-[120ms] ease-ds-out",
              size === "md" ? cx("h-[30px] text-ds-xs", iconOnly ? "w-[30px]" : "px-3") : cx("h-[26px] text-ds-2xs", iconOnly ? "w-[26px]" : "px-2"),
              on ? cx("bg-ds-accent text-ds-on-accent shadow-ds-accent", variant === "plain" ? "rounded-full" : "rounded-ds-sm") : "rounded-ds-sm text-ds-text-2 hover:text-ds-text",
            )}>
            {o.icon && <Icon name={o.icon} size={size === "sm" ? 13 : 14} />}{o.label}
          </button>
        );
      })}
    </div>
  );
}
