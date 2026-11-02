import { Search } from "lucide-react";
import { useId } from "react";
import { cx } from "../../ui/kit";
import { ICON } from "./tokens";

/** CommandSearch: 36 px field, 12 px radius, card fill with a hairline, 16 px search icon, 13 px placeholder. */
export function CommandSearch({ placeholder, label, value, onChange, onSubmit, className }: {
  placeholder: string; label: string; value?: string; onChange?: (v: string) => void; onSubmit?: (v: string) => void; className?: string;
}) {
  const id = useId();
  return (
    <form role="search" className={cx("relative flex h-9 w-full min-w-0 items-center sm:w-[212px]", className)}
      onSubmit={(e) => { e.preventDefault(); onSubmit?.(String(new FormData(e.currentTarget).get("q") ?? "")); }}>
      <label htmlFor={id} className="sr-only">{label}</label>
      <Search size={ICON.md} strokeWidth={ICON.stroke} aria-hidden className="pointer-events-none absolute left-3 text-r12-muted" />
      <input id={id} name="q" type="search" placeholder={placeholder} value={value} onChange={onChange ? (e) => onChange(e.target.value) : undefined}
        className="h-full w-full rounded-[12px] border border-r12-line-2 bg-r12-card pr-3 pl-9 text-[13px] text-r12-ink outline-none transition-[border-color,box-shadow] duration-150 placeholder:text-r12-muted focus:border-r12-accent focus:shadow-[0_0_0_3px_var(--r12-accent-soft)]" />
    </form>
  );
}
