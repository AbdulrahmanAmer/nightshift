// A native radio or checkbox styled as a 40 px row (docs/design/installer-ui.md section 2.3). Checked rows are sunken.
import type { ReactNode } from "react";
import { cx } from "@dash/ui/kit";

export function ChoiceRow({ type, name, checked, onChange, label, hint, trailing, disabled, className }: {
  type: "radio" | "checkbox"; name?: string; checked: boolean; onChange: (checked: boolean) => void; label: ReactNode; hint?: ReactNode; trailing?: ReactNode; disabled?: boolean; className?: string;
}) {
  return (
    <label className={cx("flex min-h-10 cursor-pointer items-start gap-3 rounded-ds-md px-3 py-2.5 text-ds-sm transition-colors duration-[120ms] ease-ds-out hover:bg-ds-hover has-[:focus-visible]:shadow-ds-focus", checked && "bg-ds-sunken", disabled && "cursor-not-allowed opacity-60", className)}>
      <input type={type} name={name} checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 size-4 shrink-0 accent-[var(--ds-bg-accent)] outline-none" />
      <span className="min-w-0 flex-1">
        <span className="block font-medium">{label}</span>
        {hint && <span className="mt-0.5 block text-ds-xs text-ds-text-2">{hint}</span>}
      </span>
      {trailing && <span className="shrink-0 text-ds-sm text-ds-text-2 ds-num">{trailing}</span>}
    </label>
  );
}
