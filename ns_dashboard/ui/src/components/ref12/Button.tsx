import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";

export type ButtonVariant = "primary" | "inverse" | "soft" | "ghost" | "dashed" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const V: Record<ButtonVariant, string> = {
  primary: "bg-ds-accent text-ds-on-accent shadow-ds-control hover:bg-ds-accent-hover",   // Accept invite, Check
  inverse: "bg-ds-inverse text-ds-on-inverse hover:opacity-90",                           // Add new board, Find out more
  soft: "bg-ds-accent-soft text-ds-text shadow-ds-control hover:bg-ds-soft-hover",        // Reschedule, Export data
  ghost: "bg-transparent px-2 text-ds-text-2 hover:bg-ds-hover hover:text-ds-text",        // Edit, Share, Clear
  dashed: "border-[1.5px] border-dashed border-ds-dashed bg-ds-accent-subtle font-medium text-ds-text hover:bg-ds-accent-soft",
  danger: "bg-ds-danger-soft text-ds-danger shadow-ds-control hover:brightness-[0.97]",    // Reject, Uninstall
};
const S: Record<ButtonSize, string> = {
  sm: "h-7 rounded-ds-sm px-3 text-ds-xs",  // 28
  md: "h-9 rounded-ds-md px-4 text-ds-sm",  // 36
  lg: "h-10 rounded-ds-md px-5 text-ds-sm", // 40
};
const BASE = "ds-focus inline-flex shrink-0 items-center justify-center gap-2 leading-none font-semibold whitespace-nowrap no-underline transition-[background-color,color,opacity,transform,filter] duration-[120ms] ease-ds-out active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50";

/** Button (Planify): every text button in the reference. Leading/trailing icon, plus a trailing slot for a Badge ("xls"). */
export function Button({ variant = "primary", size = "md", leadingIcon, trailingIcon, trailing, fullWidth, className, children, type = "button", ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant; size?: ButtonSize; leadingIcon?: IconName; trailingIcon?: IconName; trailing?: ReactNode; fullWidth?: boolean;
}) {
  const is = size === "sm" ? 13 : 15;
  return (
    <button type={type} {...rest} className={cx(BASE, S[size], V[variant], fullWidth && "w-full", className)}>
      {leadingIcon && <Icon name={leadingIcon} size={is} />}
      {children != null && <span>{children}</span>}
      {trailingIcon && <Icon name={trailingIcon} size={is} />}
      {trailing}
    </button>
  );
}

/** LinkButton: a link that looks like a Button (same variants and sizes). `external` opens a new tab safely. */
export function LinkButton({ variant = "soft", size = "md", leadingIcon, trailingIcon, external, className, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & {
  variant?: ButtonVariant; size?: ButtonSize; leadingIcon?: IconName; trailingIcon?: IconName; external?: boolean;
}) {
  const is = size === "sm" ? 13 : 15;
  return (
    <a {...rest} target={external ? "_blank" : rest.target} rel={external ? "noopener noreferrer" : rest.rel} className={cx(BASE, S[size], V[variant], className)}>
      {leadingIcon && <Icon name={leadingIcon} size={is} />}
      {children != null && <span>{children}</span>}
      {trailingIcon && <Icon name={trailingIcon} size={is} />}
    </a>
  );
}
