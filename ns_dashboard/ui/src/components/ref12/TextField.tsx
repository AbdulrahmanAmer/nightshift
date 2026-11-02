import type { InputHTMLAttributes } from "react";
import { cx } from "../../ui/kit";

/** TextField (Planify): the inline quick-reply field inside a message notification.
 *  30 px, radius sm, accent-subtle fill, 11 px text; focus shows the focus border. */
export function TextField({ label, className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { label: string }) {
  return (
    <label className={cx("block", className)}>
      <span className="sr-only">{label}</span>
      <input {...rest} className="h-[30px] w-full rounded-ds-sm border border-transparent bg-ds-accent-subtle px-3 text-ds-2xs text-ds-text outline-none placeholder:text-ds-muted focus-visible:border-ds-focus" />
    </label>
  );
}
