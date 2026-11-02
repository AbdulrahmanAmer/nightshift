import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import type { Tone } from "./tokens";

/** Chip: 24 px tall, 8 px radius, 12 px medium.
 *  soft = tinted fill with tone ink ("High"); solid = tone fill with white text ("Package design"). */
export function Chip({ children, tone = "accent", variant = "soft", className }: { children: ReactNode; tone?: Exclude<Tone, "ink">; variant?: "soft" | "solid"; className?: string }) {
  const soft = { accent: "bg-r12-accent-soft text-r12-accent-ink", green: "bg-r12-green-soft text-r12-green-ink", red: "bg-r12-red-soft text-r12-red-ink", muted: "bg-r12-tint-2 text-r12-text-2" }[tone];
  const solid = { accent: "bg-r12-accent text-white", green: "bg-r12-green text-white", red: "bg-r12-red text-white", muted: "bg-r12-muted text-white" }[tone];
  return <span className={cx("inline-flex h-6 shrink-0 items-center rounded-r12-chip px-3 text-[12px] leading-none font-medium whitespace-nowrap", variant === "soft" ? soft : solid, className)}>{children}</span>;
}

/** Dot: 6 px status dot used in meta lines and titles. */
export function Dot({ tone = "accent", size = 6, className }: { tone?: Tone; size?: 6 | 8 | 10; className?: string }) {
  const c = { accent: "bg-r12-accent", green: "bg-r12-green", red: "bg-r12-red", muted: "bg-r12-muted", ink: "bg-r12-ink" }[tone];
  return <span aria-hidden className={cx("inline-block shrink-0 rounded-full", c, className)} style={{ width: size, height: size }} />;
}
