import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../../ui/kit";

export type ActionVariant = "ink" | "accent" | "soft" | "ghost";
export type ActionSize = "sm" | "md";

const V: Record<ActionVariant, string> = {
  ink: "bg-r12-ink text-r12-on-ink hover:opacity-90",            // "Add new board", "Find out more"
  accent: "bg-r12-accent text-r12-on-accent hover:brightness-110", // "Accept invite", "Check"
  soft: "bg-r12-tint-2 text-r12-ink hover:brightness-[0.97]",     // "Reschedule", Export tray
  ghost: "text-r12-text-2 hover:bg-r12-tint-2 hover:text-r12-ink", // "Edit", "Share", "Clear"
};
const S: Record<ActionSize, string> = {
  sm: "h-7 gap-1.5 rounded-r12-chip px-3 text-[12px]",
  md: "h-9 gap-2 rounded-r12-control px-4 text-[13px]",
};

/** ActionButton: every text button in the reference.
 *  md = 36 px tall, 10 px radius, 13 px medium. sm = 28 px, 8 px radius, 12 px. */
export function ActionButton({ variant = "ink", size = "md", icon, children, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ActionVariant; size?: ActionSize; icon?: ReactNode }) {
  return (
    <button type="button" {...rest} className={cx(
      "r12-focus inline-flex shrink-0 items-center justify-center font-medium whitespace-nowrap select-none transition-[background-color,color,opacity,filter,transform] duration-150 ease-out active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40",
      V[variant], S[size], className,
    )}>
      {icon}{children}
    </button>
  );
}
