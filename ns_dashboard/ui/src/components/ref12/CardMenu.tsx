import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";
import { IconButton } from "./IconButton";

export interface MenuItem { label: string; icon?: IconName; onSelect: () => void; danger?: boolean }

/** CardMenu: the card kebab as a real menu. A sm IconButton opens a popover (surface, radius md, elevation 3) of
 *  menuitems; arrows move, Home/End jump, Enter/Space select, Esc or a click outside closes and focus returns to the
 *  button. Cards pass only actions the page can really do; with no items nothing renders (no dead kebab). */
export function CardMenu({ items, label, icon = "more", size = "sm", align = "end", className }: {
  items: MenuItem[]; label: string; icon?: IconName; size?: "sm" | "md"; align?: "start" | "end"; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null); const btn = useRef<HTMLButtonElement>(null);
  const id = useId();
  const focusItem = (i: number) => { const els = wrap.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]"); if (els?.length) els[(i + els.length) % els.length]!.focus(); };
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    addEventListener("pointerdown", away); const t = setTimeout(() => focusItem(0), 0);
    return () => { removeEventListener("pointerdown", away); clearTimeout(t); };
  }, [open]);
  if (!items.length) return null;
  const onKey = (e: KeyboardEvent) => {
    const els = Array.from(wrap.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]") || []); const i = els.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown") { e.preventDefault(); focusItem(i + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); focusItem(i - 1); }
    else if (e.key === "Home") { e.preventDefault(); focusItem(0); }
    else if (e.key === "End") { e.preventDefault(); focusItem(-1); }
    else if (e.key === "Escape" || e.key === "Tab") { if (e.key === "Escape") e.preventDefault(); setOpen(false); btn.current?.focus(); }
  };
  return (
    <div ref={wrap} className={cx("relative", className)} onKeyDown={open ? onKey : undefined}>
      <IconButton ref={btn} icon={icon} label={label} size={size} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => setOpen((o) => !o)} />
      {open && (
        <div id={id} role="menu" aria-label={label} className={cx("fade-in absolute top-full z-30 mt-1 flex min-w-[200px] flex-col rounded-ds-md bg-ds-surface p-1 shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]", align === "end" ? "right-0" : "left-0")}>
          {items.map((it) => (
            <button key={it.label} type="button" role="menuitem" tabIndex={-1} onClick={() => { setOpen(false); btn.current?.focus(); it.onSelect(); }}
              className={cx("flex h-9 items-center gap-2.5 rounded-ds-sm px-3 text-left text-ds-sm font-medium whitespace-nowrap outline-none hover:bg-ds-hover focus-visible:bg-ds-hover", it.danger ? "text-ds-danger" : "text-ds-text")}>
              {it.icon && <Icon name={it.icon} size={15} className={it.danger ? undefined : "text-ds-text-2"} />}{it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
