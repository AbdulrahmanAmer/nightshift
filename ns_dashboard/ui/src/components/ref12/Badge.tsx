import type { HTMLAttributes } from "react";
import { cx } from "../../ui/kit";

export type Tone = "accent" | "success" | "danger" | "neutral";

const SOLID: Record<Tone, string> = { accent: "bg-ds-accent text-ds-on-accent", success: "bg-ds-success text-white", danger: "bg-ds-danger text-white", neutral: "bg-ds-text-2 text-ds-surface" };
const SOFT: Record<Tone, string> = { accent: "bg-ds-accent-soft text-ds-accent-ink", success: "bg-ds-success-soft text-ds-success-ink", danger: "bg-ds-danger-soft text-ds-danger", neutral: "bg-ds-sunken text-ds-text-2" };
const SIZE = {
  xs: "h-4 rounded-full px-[5px] text-[10px] tracking-[0.02em]", // "xls", "NEW"
  sm: "h-[22px] rounded-ds-xs px-2 text-ds-2xs",                // "Motion design"
  md: "h-7 rounded-ds-sm px-3 text-ds-xs",                       // "High", "Package design"
} as const;

/** Badge (Planify): chips, tags, priority labels and micro badges.
 *  solid = filled chip ("Package design"), soft = tinted label ("High"). */
export function Badge({ tone = "accent", variant = "soft", size = "sm", className, ...rest }: HTMLAttributes<HTMLSpanElement> & { tone?: Tone; variant?: "solid" | "soft"; size?: keyof typeof SIZE }) {
  return <span {...rest} className={cx("inline-flex items-center gap-1 leading-none font-semibold whitespace-nowrap", SIZE[size], variant === "solid" ? SOLID[tone] : SOFT[tone], className)} />;
}
