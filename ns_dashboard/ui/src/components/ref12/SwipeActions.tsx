import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import type { IconName } from "./Icon";
import { IconButton } from "./IconButton";

export interface SwipeAction { icon: IconName; label: string; onClick?: () => void }

/** SwipeActions (Planify): an action tray (sunken, radius lg, icons stacked right, 12 px apart) behind a list item.
 *  open = the content slides 52 px left (transform) and the tray fades in; closed tray buttons leave the tab order. */
export function SwipeActions({ actions, open = false, children, className }: { actions: SwipeAction[]; open?: boolean; children: ReactNode; className?: string }) {
  return (
    <div className={cx("relative rounded-ds-lg", className)}>
      <div aria-hidden={!open} className={cx("absolute inset-0 flex flex-col items-end justify-center gap-3 rounded-[inherit] bg-ds-sunken pr-3 transition-opacity duration-200 ease-ds-out", open ? "opacity-100" : "opacity-0")}>
        {actions.map((a) => <IconButton key={a.label} icon={a.icon} label={a.label} size="sm" onClick={a.onClick} tabIndex={open ? 0 : -1} className="!text-ds-text" />)}
      </div>
      <div className={cx("relative transition-transform duration-200 ease-ds-out", open && "-translate-x-[52px]")}>{children}</div>
    </div>
  );
}
