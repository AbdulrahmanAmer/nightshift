// A text field with a visible label on its left (compact cards) or above it. Built on the ref12 Input recipe.
import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import { Input } from "@ref12";
import { cx } from "@dash/ui/kit";

export function LabeledField({ label, error, hint, inline, className, inputClassName, trailing, ...rest }: InputHTMLAttributes<HTMLInputElement> & {
  label: string; error?: string | null; hint?: ReactNode; inline?: boolean; inputClassName?: string; trailing?: ReactNode;
}) {
  const id = useId();
  const msg = `${id}-msg`;
  return (
    <div className={cx("min-w-0", inline ? "grid grid-cols-[104px_minmax(0,1fr)] items-start gap-x-3" : "flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className={cx("text-ds-sm font-medium text-ds-text-2", inline && "flex h-9 items-center")}>{label}</label>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <Input id={id} {...rest} aria-invalid={error ? true : undefined} aria-describedby={error || hint ? msg : undefined} className={cx("h-9", error && "border-ds-danger", inputClassName)} />
          {trailing}
        </div>
        {(error || hint) && <p id={msg} className={cx("mt-1 text-ds-xs", error ? "text-ds-danger" : "text-ds-muted")}>{error ?? hint}</p>}
      </div>
    </div>
  );
}
