import { useId, type ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Card } from "./Card";
import { Icon, type IconName } from "./Icon";

/* ---------------------------------------------------------------- AppTile, AppGrid (Planify Controls.tsx) */

export interface AppTileProps {
  icon: IconName;
  label: string;
  /** Replaces the icon box with a small picture (a layout diagram). */
  art?: ReactNode;
  /** Second line under the label. */
  caption?: string;
  /** Selected state: the tile presses in and the glyph takes the selection colour (aria-checked in a radio AppGrid). */
  selected?: boolean;
  onClick?: () => void;
  className?: string;
}

/** AppTile: a raised square with an accent glyph and its label under it. Presses in on :active and when selected. */
export function AppTile({ icon, label, art, caption, selected, onClick, className, role }: AppTileProps & { role?: "radio" }) {
  return (
    <button type="button" role={role} aria-checked={role ? !!selected : undefined} onClick={onClick}
      className={cx("ds-focus flex min-h-[104px] min-w-0 flex-col items-center justify-center gap-2 rounded-ds-lg px-3 py-4 text-center transition-[box-shadow,transform,background-color] duration-200 ease-ds-out",
        selected ? "bg-ds-accent-subtle text-ds-text shadow-[var(--ds-shadow-inset),0_0_0_1.5px_var(--ds-border-focus)]" : "bg-ds-surface text-ds-text shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)] hover:-translate-y-px active:translate-y-0 active:shadow-ds-inset", className)}>
      {art ? <span aria-hidden className="grid place-items-center">{art}</span> : <span aria-hidden className={cx("grid size-10 place-items-center rounded-ds-md", selected ? "bg-ds-accent text-ds-on-accent shadow-ds-accent" : "bg-ds-accent-soft text-ds-accent-ink")}><Icon name={icon} size={20} /></span>}
      <span className="text-ds-sm leading-ds-snug font-semibold">{label}</span>
      {caption && <span className="line-clamp-2 text-ds-2xs leading-ds-snug text-ds-muted">{caption}</span>}
    </button>
  );
}

/** AppGrid: AppTiles in a 2-column grid (3 from 520 px of container, 4 from 760). `choice` makes it a radio group;
 *  `bare` drops the card so it can sit inside another card. */
export function AppGrid({ apps, label, choice, bare, className }: { apps: (AppTileProps & { id: string })[]; label: string; choice?: boolean; bare?: boolean; className?: string }) {
  const grid = (
    <div role={choice ? "radiogroup" : undefined} aria-label={label} className="grid grid-cols-2 gap-3 @min-[520px]:grid-cols-3 @min-[760px]:grid-cols-4">
      {apps.map(({ id, ...a }) => <AppTile key={id} {...a} role={choice ? "radio" : undefined} />)}
    </div>
  );
  if (bare) return <div className={cx("@container", className)}>{grid}</div>;
  return <Card as="nav" padding="md" aria-label={label} className={cx("@container", className)}>{grid}</Card>;
}

/* ---------------------------------------------------------------- KnobSwitch */

/** KnobSwitch (Planify): an oversized switch. Inset track carrying the current label; a raised round knob with an
 *  icon slides across. Off shows `offLabel` on the right of the knob, on shows `onLabel` on its left. */
export function KnobSwitch({ checked, onChange, label, onLabel, offLabel, onIcon, offIcon, className }: {
  checked: boolean; onChange: (v: boolean) => void; label: string; onLabel: string; offLabel: string; onIcon: IconName; offIcon: IconName; className?: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} onClick={() => onChange(!checked)}
      className={cx("ds-focus relative flex h-12 w-full min-w-[200px] items-center rounded-full bg-ds-sunken px-5 text-left shadow-[var(--ds-shadow-inset),0_0_0_1px_var(--ds-border-subtle)]", className)}>
      <span aria-hidden className={cx("flex-1 text-ds-md font-semibold transition-[padding,color] duration-200 ease-ds-out", checked ? "pr-12 text-ds-text" : "pl-12 text-ds-text-2")}>{checked ? onLabel : offLabel}</span>
      <span aria-hidden className={cx("absolute top-1/2 grid size-14 -translate-y-1/2 place-items-center rounded-full bg-ds-surface text-ds-accent-ink shadow-[var(--ds-shadow-2),0_0_0_1px_var(--ds-border-subtle)] transition-[left] duration-200 ease-ds-out",
        checked ? "left-[calc(100%-52px)]" : "-left-1")}>
        <Icon name={checked ? onIcon : offIcon} size={20} />
      </span>
    </button>
  );
}

/* ---------------------------------------------------------------- IntakeStepper */

/** IntakeStepper (Planify): minus and plus around a vessel that fills as the count rises, with "n/max" on it.
 *  Here it doubles as the wizard step control: minus goes back, plus goes on. `canIncrement` gates the plus. */
export function IntakeStepper({ title, value, max, onChange, decLabel, incLabel, canIncrement = true, caption, className, bare }: {
  title: ReactNode; value: number; max: number; onChange: (v: number) => void; decLabel: string; incLabel: string; canIncrement?: boolean; caption?: ReactNode; className?: string; bare?: boolean;
}) {
  const id = useId();
  const level = Math.max(0, Math.min(1, value / max));
  const step = "ds-focus grid size-9 place-items-center rounded-full text-ds-accent-ink transition-[box-shadow,background-color] duration-[120ms] ease-ds-out hover:enabled:bg-ds-surface hover:enabled:shadow-[var(--ds-shadow-control),0_0_0_1px_var(--ds-border-default)] active:enabled:shadow-ds-inset-sm disabled:cursor-not-allowed disabled:opacity-35";
  const body = (
    <>
      <div className="min-w-0 flex-1">
        <h2 id={id} className="truncate text-ds-md leading-ds-snug font-semibold">{title}</h2>
        {caption && <p className="truncate text-ds-2xs text-ds-muted">{caption}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button type="button" aria-label={decLabel} title={decLabel} disabled={value <= 1} onClick={() => onChange(value - 1)} className={step}><Icon name="minus" size={18} strokeWidth={2.2} /></button>
        <span className="relative grid h-12 w-10 place-items-center overflow-hidden rounded-ds-sm bg-ds-sunken shadow-[var(--ds-shadow-inset-sm),0_0_0_1.5px_var(--ds-border-dashed)]">
          <span aria-hidden className="absolute inset-0 origin-bottom bg-ds-accent-soft transition-transform duration-200 ease-ds-out" style={{ transform: `scaleY(${level})` }} />
          <output aria-live="polite" className="relative text-ds-xs font-semibold ds-num">{value}/{max}</output>
        </span>
        <button type="button" aria-label={incLabel} title={incLabel} disabled={value >= max || !canIncrement} onClick={() => onChange(value + 1)} className={step}><Icon name="plus" size={18} strokeWidth={2.2} /></button>
      </div>
    </>
  );
  if (bare) return <div role="group" aria-labelledby={id} className={cx("flex items-center gap-4", className)}>{body}</div>;
  return <Card as="section" padding="md" aria-labelledby={id} className={cx("flex items-center gap-4", className)}>{body}</Card>;
}
