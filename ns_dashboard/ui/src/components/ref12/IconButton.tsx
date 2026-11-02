import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";

const V = {
  ghost: "bg-transparent text-ds-text-2 hover:bg-ds-hover hover:text-ds-text",
  soft: "bg-ds-sunken text-ds-text-2 hover:text-ds-text",
  surface: "bg-ds-surface text-ds-text shadow-ds-1",
  accent: "bg-ds-accent text-ds-on-accent shadow-ds-accent hover:bg-ds-accent-hover",
} as const;
const S = { sm: "size-6 rounded-ds-xs", md: "size-9 rounded-ds-sm", lg: "size-11 rounded-ds-md" } as const;

/** IconButton (Planify): bell, gear, "…", calendar pager. `label` is required (no visible text).
 *  sm 24 / md 36 / lg 44; `dot` adds the amber notification dot. */
export type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { icon: IconName; label: string; variant?: keyof typeof V; size?: keyof typeof S; round?: boolean; dot?: boolean };
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton({ icon, label, variant = "ghost", size = "md", round, dot, className, type = "button", ...rest }, ref) {
  return (
    <button ref={ref} type={type} aria-label={label} title={label} {...rest}
      className={cx("ds-focus relative inline-grid shrink-0 place-items-center transition-colors duration-[120ms] ease-ds-out", S[size], V[variant], round && "!rounded-full", className)}>
      <Icon name={icon} size={size === "sm" ? 14 : size === "lg" ? 20 : 18} />
      {dot && <span aria-hidden className="absolute top-[7px] right-2 size-[7px] rounded-full bg-ds-attention shadow-[0_0_0_2px_var(--ds-bg-app)]" />}
    </button>
  );
});
