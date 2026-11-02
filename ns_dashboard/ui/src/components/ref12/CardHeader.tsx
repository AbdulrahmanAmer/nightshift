import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** CardHeader (Planify): xl/600 title (lg for level 3), tight leading, -0.01em, 20 px below.
 *  `adornment` follows the title (AvatarGroup); `actions` sit right with -8 px so ghost buttons align to the edge. */
export function CardHeader({ title, id, level = 2, actions, adornment, className }: { title: ReactNode; id?: string; level?: 2 | 3; actions?: ReactNode; adornment?: ReactNode; className?: string }) {
  const H = level === 2 ? "h2" : "h3";
  return (
    <header className={cx("mb-5 flex flex-wrap items-center justify-between gap-3", className)}>
      <div className="flex min-w-0 items-center gap-3">
        <H id={id} className={cx("truncate leading-ds-tight font-semibold tracking-[-0.01em]", level === 2 ? "text-ds-xl" : "text-ds-lg")}>{title}</H>
        {adornment}
      </div>
      {actions && <div className="-mr-2 flex flex-wrap items-center gap-1">{actions}</div>}
    </header>
  );
}
