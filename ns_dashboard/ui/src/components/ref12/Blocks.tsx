import { useRef, useState, type ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";
import { StatusDot, type DotTone } from "./StatusDot";

/* Page building blocks that the pages share, all on the ref12 tokens. */

/** DashGrid: the recreation's container-query grid (.ds-dash rules) without fixed areas: 1 column, 4 from 600 px of
 *  container, 6 from 1020 px, 20 px gaps. Children pick their width with `span` classes from SPAN below. */
export function DashGrid({ children, className, label }: { children: ReactNode; className?: string; label?: string }) {
  return <div className="@container"><div aria-label={label} className={cx("grid grid-cols-1 items-start gap-5 @min-[600px]:grid-cols-4 @min-[1020px]:grid-cols-6", className)}>{children}</div></div>;
}
/** Span presets, written out so Tailwind sees every class. Key = columns at 6 / at 4. */
export const SPAN = {
  "1/1": "@min-[600px]:col-span-1 @min-[1020px]:col-span-1",
  "1/2": "@min-[600px]:col-span-2 @min-[1020px]:col-span-1",
  "2/2": "@min-[600px]:col-span-2 @min-[1020px]:col-span-2",
  "2/4": "@min-[600px]:col-span-4 @min-[1020px]:col-span-2",
  "3/4": "@min-[600px]:col-span-4 @min-[1020px]:col-span-3",
  "3/2": "@min-[600px]:col-span-2 @min-[1020px]:col-span-3",
  "4/4": "@min-[600px]:col-span-4 @min-[1020px]:col-span-4",
  "6/4": "@min-[600px]:col-span-4 @min-[1020px]:col-span-6",
} as const;
export type Span = keyof typeof SPAN;

/** PageIntro: the page's own heading row inside the window (2xl/600 title, md secondary line, actions right). */
export function PageIntro({ title, sub, actions, className }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <header className={cx("flex flex-wrap items-end justify-between gap-x-6 gap-y-3", className)}>
      <div className="min-w-0">
        <h1 className="text-ds-2xl leading-ds-tight font-semibold tracking-[-0.02em]">{title}</h1>
        {sub && <p className="mt-2 max-w-[64ch] text-ds-md leading-ds-normal text-ds-text-2">{sub}</p>}
      </div>
      {actions && <div className="flex max-w-full min-w-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** StatusChip: the top bar status pill (36 px, radius md, surface, default border) with a StatusDot. */
export function StatusChip({ tone, children, live, className, title }: { tone: DotTone | "muted"; children: ReactNode; live?: boolean; className?: string; title?: string }) {
  return (
    <span role={live ? "status" : undefined} aria-live={live ? "polite" : undefined} title={title}
      className={cx("inline-flex h-9 min-w-0 items-center gap-2 rounded-ds-md border border-ds-border bg-ds-surface px-3 text-ds-xs font-medium whitespace-nowrap text-ds-text shadow-ds-0", className)}>
      {tone === "muted" ? <span aria-hidden className="size-[7px] shrink-0 rounded-full bg-ds-muted" /> : <StatusDot tone={tone} />}
      <span className="truncate">{children}</span>
    </span>
  );
}

/** Kbd: a keycap (Badge xs recipe on the sunken fill). */
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="inline-grid h-5 min-w-5 place-items-center rounded-ds-xs bg-ds-sunken px-1 font-[inherit] text-[10px] leading-none font-semibold text-ds-text-2 shadow-[inset_0_-1px_0_var(--ds-border-default)]">{children}</kbd>;
}

/** EmptyState: the CreateTile's dashed accent-subtle tile used as a message, with an optional action. */
export function EmptyState({ title, children, action, icon, className }: { title: string; children?: ReactNode; action?: ReactNode; icon?: IconName; className?: string }) {
  return (
    <div className={cx("flex flex-col items-center justify-center gap-2 rounded-ds-lg border-[1.5px] border-dashed border-ds-dashed bg-ds-accent-subtle px-6 py-10 text-center", className)}>
      {icon && <span aria-hidden className="mb-1 grid size-10 place-items-center rounded-ds-md bg-ds-surface text-ds-accent-ink shadow-ds-1"><Icon name={icon} size={18} /></span>}
      <p className="text-ds-md font-semibold">{title}</p>
      {children && <p className="max-w-[44ch] text-ds-sm leading-ds-normal text-ds-text-2">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/** DropTile: CreateTile's dashed recipe as a file drop zone (a real file input inside a label). */
export function DropTile({ title, sub, accept, onFile, chooseLabel = "Choose a file", compact, disabled, className }: {
  title: string; sub?: string; accept: string; onFile: (f: File) => void; chooseLabel?: string; compact?: boolean; disabled?: boolean; className?: string;
}) {
  const [over, setOver] = useState(false); const input = useRef<HTMLInputElement>(null);
  return (
    <label onDragEnter={(e) => { e.preventDefault(); setOver(true); }} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files?.[0]; if (f && !disabled) onFile(f); }}
      className={cx("flex cursor-pointer flex-col items-center justify-center gap-2 rounded-ds-lg border-[1.5px] border-dashed text-center transition-[background-color,border-color] duration-[120ms] ease-ds-out focus-within:shadow-ds-focus",
        compact ? "px-4 py-6" : "px-6 py-10", over ? "border-ds-accent bg-ds-accent-soft" : "border-ds-dashed bg-ds-accent-subtle hover:bg-ds-accent-soft", disabled && "pointer-events-none opacity-60", className)}>
      <span aria-hidden className="grid size-[26px] place-items-center rounded-ds-sm bg-ds-accent text-ds-on-accent shadow-ds-accent"><Icon name="upload" size={14} strokeWidth={2.2} /></span>
      <span className="text-ds-md font-semibold">{title}</span>
      {sub && <span className="max-w-[48ch] text-ds-xs leading-ds-normal text-ds-text-2">{sub}</span>}
      <span className="mt-1 inline-flex h-7 items-center rounded-ds-sm bg-ds-surface px-3 text-ds-xs font-semibold shadow-[var(--ds-shadow-control),0_0_0_1px_var(--ds-border-default)]">{chooseLabel}</span>
      <input ref={input} type="file" accept={accept} disabled={disabled} className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ""; }} />
    </label>
  );
}

/** StepItem: one numbered step. Number circle (28 px; done = accent with a check, current = accent ring),
 *  sm/600 title, 2xs muted sub, an action on the right. `dimmed` greys it out until an earlier step is done. */
export function StepItem({ n, title, sub, done, current, dimmed, action, children, className }: {
  n: number; title: ReactNode; sub?: ReactNode; done?: boolean; current?: boolean; dimmed?: boolean; action?: ReactNode; children?: ReactNode; className?: string;
}) {
  return (
    <li aria-current={current ? "step" : undefined} aria-disabled={dimmed || undefined}
      className={cx("rounded-ds-lg px-3 py-2.5 transition-[opacity,background-color,box-shadow] duration-200 ease-ds-out", current && "bg-ds-surface shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)]", dimmed && "opacity-50", className)}>
      <div className="flex items-center gap-3">
        <StepNumber n={n} done={done} current={current} />
        <div className="min-w-0 flex-1">
          <p className="text-ds-sm leading-ds-snug font-semibold">{title}</p>
          {sub && <p className="truncate text-ds-2xs leading-ds-snug text-ds-muted" title={typeof sub === "string" ? sub : undefined}>{sub}</p>}
        </div>
        {action && <fieldset disabled={dimmed} className="contents">{action}</fieldset>}
      </div>
      {children && <div className="mt-3 pl-10">{children}</div>}
    </li>
  );
}
export function StepNumber({ n, done, current }: { n: number; done?: boolean; current?: boolean }) {
  return (
    <span className={cx("grid size-7 shrink-0 place-items-center rounded-full text-ds-xs font-semibold ds-num transition-colors duration-200",
      done ? "bg-ds-accent text-ds-on-accent" : current ? "bg-ds-accent-soft text-ds-accent-ink shadow-[inset_0_0_0_1.5px_var(--ds-border-focus)]" : "bg-ds-sunken text-ds-text-2 shadow-[inset_0_0_0_1px_var(--ds-border-default)]")}>
      {done ? <><Icon name="check" size={14} strokeWidth={2.2} /><span className="sr-only">Done</span></> : n}
    </span>
  );
}

/** StepRail: the wizard's step list. Each step is a button (jump back to any reached step); the current one is raised. */
export function StepRail({ steps, current, reached, onPick, label, className }: { steps: { title: string; sub?: string }[]; current: number; reached: number; onPick: (i: number) => void; label: string; className?: string }) {
  return (
    <ol aria-label={label} className={cx("flex flex-col gap-1", className)}>
      {steps.map((s, i) => {
        const on = i === current; const can = i <= reached;
        return (
          <li key={s.title}>
            <button type="button" disabled={!can} aria-current={on ? "step" : undefined} onClick={() => onPick(i)}
              className={cx("ds-focus flex w-full items-center gap-3 rounded-ds-lg px-3 py-2.5 text-left transition-[background-color,box-shadow] duration-200 ease-ds-out disabled:cursor-not-allowed disabled:opacity-50",
                on ? "bg-ds-surface shadow-[var(--ds-shadow-2),0_0_0_1px_var(--ds-border-subtle)]" : "hover:enabled:bg-ds-hover")}>
              <StepNumber n={i + 1} done={i < current && can} current={on} />
              <span className="min-w-0"><span className="block truncate text-ds-sm font-semibold">{s.title}</span>{s.sub && <span className="block truncate text-ds-2xs text-ds-muted">{s.sub}</span>}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** SettingRow: a settings list row. sm/600 title and xs secondary text left, the control right; hairline between rows. */
export function SettingRow({ title, sub, children, className }: { title: ReactNode; sub?: ReactNode; children?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 border-b border-ds-border-subtle py-4 last:border-0 sm:flex-row sm:items-center sm:justify-between sm:gap-6", className)}>
      <div className="min-w-0"><p className="text-ds-sm font-semibold">{title}</p>{sub && <div className="mt-0.5 text-ds-xs leading-ds-normal text-ds-text-2">{sub}</div>}</div>
      {children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

/** Figure: a stat number tile without a ring (label, big tabular figure, a comparison line). */
export function Figure({ label, value, detail, className }: { label: string; value: ReactNode; detail?: ReactNode; className?: string }) {
  return (
    <div className={cx("rounded-ds-xl bg-ds-surface px-5 py-4 shadow-ds-1", className)}>
      <p className="text-ds-xs text-ds-text-2">{label}</p>
      <p className="mt-2 text-[2rem] leading-none font-semibold tracking-[-0.01em] ds-num">{value}</p>
      {detail && <p className="mt-2 text-ds-2xs text-ds-muted ds-num">{detail}</p>}
    </div>
  );
}
