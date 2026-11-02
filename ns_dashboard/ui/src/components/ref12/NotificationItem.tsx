import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { IconButton } from "./IconButton";

/** NotificationItem (Planify): radius lg surface row, padding 12/12/12/16.
 *  resting = hairline ring + elevation 1; raised = elevation 3 (floats over its SwipeActions tray).
 *  Title md/600 with an inline adornment (StatusDot); 2xs muted subtitle; a meta row of MetaItems
 *  (8 × 24 gaps, ink); optional body such as a TextField. */
export function NotificationItem({ title, titleAdornment, subtitle, meta, children, moreLabel, onMore, state = "resting", className }: {
  title: ReactNode; titleAdornment?: ReactNode; subtitle?: ReactNode; meta?: ReactNode; children?: ReactNode;
  moreLabel?: string; onMore?: () => void; state?: "resting" | "raised"; className?: string;
}) {
  return (
    <article className={cx("rounded-ds-lg bg-ds-surface py-3 pr-3 pl-4 transition-shadow duration-200 ease-ds-out",
      state === "raised" ? "shadow-ds-3" : "shadow-[0_0_0_1px_var(--ds-border-subtle),var(--ds-shadow-1)]", className)}>
      <header className="flex items-center justify-between gap-2">
        <h3 className="inline-flex min-w-0 items-center gap-1.5 text-ds-md leading-ds-snug font-semibold"><span className="truncate">{title}</span>{titleAdornment}</h3>
        {onMore && <IconButton icon="more" label={moreLabel ?? "More options"} size="sm" onClick={onMore} />}
      </header>
      {subtitle && <p className="text-ds-2xs text-ds-muted">{subtitle}</p>}
      {meta && <div className="mt-3 flex flex-wrap gap-x-6 gap-y-2 [&>span]:text-ds-text">{meta}</div>}
      {children && <div className="mt-3">{children}</div>}
    </article>
  );
}
