import type { InputHTMLAttributes } from "react";
import { cx } from "../../ui/kit";
import { Icon } from "./Icon";

/** SearchInput (Planify): the top bar command field. 36 px, radius md, surface fill, default border;
 *  focus = focus border + ring. Optional keyboard hint at the end. */
export function SearchInput({ label, shortcut, className, ...rest }: Omit<InputHTMLAttributes<HTMLInputElement>, "size"> & { label: string; shortcut?: string }) {
  return (
    <label className={cx("flex h-9 min-w-0 items-center gap-2 rounded-ds-md border border-ds-border bg-ds-surface px-3 text-ds-muted transition-[border-color,box-shadow] duration-[120ms] ease-ds-out focus-within:border-ds-focus focus-within:shadow-ds-focus", className)}>
      <Icon name="search" size={15} />
      <span className="sr-only">{label}</span>
      <input type="search" {...rest} className="min-w-0 flex-1 border-0 bg-transparent text-ds-xs text-ds-text outline-none placeholder:text-ds-muted [&::-webkit-search-cancel-button]:hidden" />
      {shortcut && <kbd className="rounded-ds-xs bg-ds-sunken px-1.5 py-0.5 font-[inherit] text-ds-2xs">{shortcut}</kbd>}
    </label>
  );
}
