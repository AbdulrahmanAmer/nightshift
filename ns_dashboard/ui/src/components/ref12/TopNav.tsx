import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

export interface TopNavTab { id: string; label: string; href: string; icon?: ReactNode }

/** TopNav: the text tabs at the top of the window. 14 px medium labels with a 16 px icon,
 *  24 px between tabs, active tab = ink text with a 2 px ink underline 6 px below the baseline box. */
export function TopNav({ tabs, active, label, className }: { tabs: TopNavTab[]; active: string; label: string; className?: string }) {
  return (
    <nav aria-label={label} className={cx("min-w-0 overflow-x-auto [scrollbar-width:none]", className)}>
      <ul className="flex items-center gap-6">
        {tabs.map((t) => {
          const on = t.id === active;
          return (
            <li key={t.id}>
              <a href={t.href} aria-current={on ? "page" : undefined}
                className={cx("r12-focus relative flex h-9 items-center gap-1.5 rounded-[4px] text-[14px] font-medium whitespace-nowrap no-underline transition-colors duration-150",
                  on ? "text-r12-ink" : "text-r12-text-2 hover:text-r12-ink")}>
                {t.icon}{t.label}
                <span aria-hidden className={cx("absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-r12-ink transition-opacity duration-150", on ? "opacity-100" : "opacity-0")} />
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** TopBar: TopNav on the left, actions on the right. Wraps under 900 px instead of squeezing. */
export function TopBar({ nav, search, actions, className }: { nav: ReactNode; search?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <header className={cx("flex flex-wrap items-center gap-x-8 gap-y-3", className)}>
      {nav}
      {search}
      <div className="flex flex-1 flex-wrap items-center justify-end gap-2.5">{actions}</div>
    </header>
  );
}
