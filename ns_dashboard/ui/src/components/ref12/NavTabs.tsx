import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";

export interface NavTab<T extends string> { id: T; label: string; icon: IconName; href?: string }

/** NavTabs (Planify): icon + label links, 24 px apart, sm/600 ink. Inactive at 78% opacity;
 *  the current link carries aria-current and a 2 px currentColor underline. */
export function NavTabs<T extends string>({ tabs, active, onChange, label, className }: { tabs: readonly NavTab<T>[]; active: T; onChange?: (id: T) => void; label: string; className?: string }) {
  return (
    <nav aria-label={label} className={cx("flex min-w-0 items-center gap-6 overflow-x-auto [scrollbar-width:none]", className)}>
      {tabs.map((t) => {
        const on = t.id === active;
        return (
          <a key={t.id} href={t.href ?? `#${t.id}`} aria-current={on ? "page" : undefined}
            onClick={(e) => { if (onChange) { e.preventDefault(); onChange(t.id); } }}
            className={cx("ds-focus relative inline-flex items-center gap-1.5 rounded-ds-xs py-2 text-ds-sm font-semibold whitespace-nowrap text-ds-text no-underline transition-opacity duration-[120ms]",
              on ? "after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:rounded-[2px] after:bg-current" : "opacity-[0.78] hover:opacity-100")}>
            <Icon name={t.icon} size={15} />{t.label}
          </a>
        );
      })}
    </nav>
  );
}
