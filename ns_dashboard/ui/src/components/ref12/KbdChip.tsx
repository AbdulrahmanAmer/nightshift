import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** KbdChip: the small filled chip at the end of ExportButton (".xls" in the reference).
 *  22 px tall, 6 px radius, light purple fill, 11 px white mono text. */
export function KbdChip({ children, className, tone = "accent" }: { children: ReactNode; className?: string; tone?: "accent" | "plain" }) {
  return (
    <kbd className={cx(
      "inline-flex h-[22px] min-w-[22px] items-center justify-center rounded-[6px] px-1.5 font-mono text-[11px] leading-none",
      tone === "accent" ? "bg-r12-accent-2 text-white" : "border border-r12-line-2 bg-r12-card text-r12-text-2",
      className,
    )}>{children}</kbd>
  );
}
