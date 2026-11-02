import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";
import { cx } from "../../ui/kit";

/* Form controls for the pages, on the SearchInput recipe: 40 px, radius md, surface fill, default border,
   focus = focus border + ring; in the soft theme the fill is a pressed-in well. */
export const FIELD = "w-full min-w-0 rounded-ds-md border border-ds-border bg-ds-surface px-3 text-ds-sm text-ds-text shadow-ds-inset-sm outline-none transition-[border-color,box-shadow] duration-[120ms] ease-ds-out placeholder:text-ds-muted focus:border-ds-focus focus:shadow-ds-focus disabled:cursor-not-allowed disabled:opacity-60 read-only:bg-ds-sunken";

/** Field: a visible sm/600 label over the control, an optional 2xs muted hint under it. */
export function Field({ label, hint, htmlFor, children, className }: { label: ReactNode; hint?: ReactNode; htmlFor?: string; children: ReactNode; className?: string }) {
  return (
    <div className={cx("flex min-w-0 flex-col gap-2", className)}>
      <label htmlFor={htmlFor} className="text-ds-sm font-semibold text-ds-text-2">{label}</label>
      {children}
      {hint && <p className="text-ds-2xs leading-ds-snug text-ds-muted">{hint}</p>}
    </div>
  );
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className={cx(FIELD, "h-10", className)} />;
}
export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...rest} className={cx(FIELD, "h-10 pr-8", className)}>{children}</select>;
}
export function TextArea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...rest} className={cx(FIELD, "min-h-20 resize-y py-2.5 leading-ds-normal", className)} />;
}

/** Switch: 44 x 24 track (inset), 18 px knob; on = accent track. role="switch", label required. */
export function Switch({ checked, onChange, label, disabled, className }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean; className?: string }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} title={label} disabled={disabled} onClick={() => onChange(!checked)}
      className={cx("ds-focus relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 ease-ds-out disabled:opacity-50",
        checked ? "bg-ds-accent" : "bg-ds-track shadow-[var(--ds-shadow-inset-sm),inset_0_0_0_1px_var(--ds-border-default)]", className)}>
      <span aria-hidden className={cx("absolute top-[3px] left-[3px] size-[18px] rounded-full bg-white shadow-[0_1px_3px_rgba(21,21,59,0.25)] transition-transform duration-200 ease-ds-out", checked && "translate-x-5")} />
    </button>
  );
}

/** Checkbox row: an 18 px native checkbox in the accent colour with a md label beside it. */
export function Checkbox({ checked, onChange, children, className }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode; className?: string }) {
  return (
    <label className={cx("flex cursor-pointer items-start gap-3 text-ds-md", className)}>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="ds-focus mt-0.5 size-[18px] shrink-0 rounded-ds-xs accent-[var(--ds-bg-accent)]" />
      <span>{children}</span>
    </label>
  );
}

/** ChoiceChip: a toggle chip for multi-pick pools (aria-pressed). On = accent soft fill with a check. */
export function ChoiceChip({ on, onChange, children, className }: { on: boolean; onChange: (v: boolean) => void; children: ReactNode; className?: string }) {
  return (
    <button type="button" aria-pressed={on} onClick={() => onChange(!on)}
      className={cx("ds-focus inline-flex h-8 items-center gap-1.5 rounded-ds-sm px-3 text-ds-xs font-semibold transition-[background-color,color,box-shadow] duration-[120ms] ease-ds-out",
        on ? "bg-ds-accent-soft text-ds-accent-ink shadow-[inset_0_0_0_1px_var(--ds-border-focus)]" : "bg-ds-surface text-ds-text-2 shadow-[var(--ds-shadow-control),0_0_0_1px_var(--ds-border-default)] hover:text-ds-text", className)}>
      {on && <svg aria-hidden width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 6.2 5 8.5l4.5-5" /></svg>}
      {children}
    </button>
  );
}
