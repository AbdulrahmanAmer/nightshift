import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** Card: the white body card. 28 px radius; 24 px padding (20 px under 640 px and on compact stat cards). */
export function Card({ children, className, pad = 24, as: As = "section", labelledBy }: { children: ReactNode; className?: string; pad?: 16 | 20 | 24; as?: "section" | "div" | "article"; labelledBy?: string }) {
  return <As aria-labelledby={labelledBy} className={cx("r12-card min-w-0", pad === 24 ? "p-5 sm:p-6" : pad === 20 ? "p-5" : "p-4", className)}>{children}</As>;
}

/** CardHeader: 20/28 semibold title with an optional inline extra (avatar stack) and a right-side action. */
export function CardHeader({ title, id, extra, action, className, size = "md" }: { title: ReactNode; id?: string; extra?: ReactNode; action?: ReactNode; className?: string; size?: "md" | "lg" }) {
  return (
    <div className={cx("flex flex-wrap items-center justify-between gap-x-3 gap-y-2", size === "lg" ? "min-h-7" : "min-h-9", className)}>
      <div className="flex min-w-0 items-center gap-3">
        <h2 id={id} className={cx("truncate font-semibold text-r12-ink", size === "lg" ? "text-[22px] leading-7" : "text-[20px] leading-7")}>{title}</h2>
        {extra}
      </div>
      {action && <div className="flex shrink-0 flex-wrap items-center gap-1">{action}</div>}
    </div>
  );
}
