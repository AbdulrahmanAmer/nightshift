import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";

/** MetaItem (Planify): 13 px muted icon + 11 px caption ("Sat, 10 May", "11 AM - 11:45 AM", attachment counts). */
export function MetaItem({ icon, children, className }: { icon?: IconName; children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 text-ds-2xs whitespace-nowrap text-ds-text-2 ds-num", className)}>
      {icon && <Icon name={icon} size={13} className="text-ds-muted" />}{children}
    </span>
  );
}
