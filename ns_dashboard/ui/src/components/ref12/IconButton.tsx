import type { ButtonHTMLAttributes, ReactNode } from "react";
import { MoreHorizontal, MoreVertical } from "lucide-react";
import { cx } from "../../ui/kit";
import { ICON } from "./tokens";

/** IconButton: bell, gear, calendar arrows, swipe-tray actions. 36 px hit area by default. */
export function IconButton({ label, children, size = 36, round, active, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode; size?: 24 | 28 | 32 | 36; round?: boolean; active?: boolean }) {
  return (
    <button type="button" aria-label={label} title={label} {...rest}
      className={cx(
        "r12-focus inline-flex shrink-0 items-center justify-center transition-colors duration-150",
        round ? "rounded-full" : "rounded-r12-control",
        active ? "bg-r12-accent text-r12-on-accent" : "text-r12-text-2 hover:bg-r12-tint-2 hover:text-r12-ink",
        className,
      )}
      style={{ width: size, height: size }}>
      {children}
    </button>
  );
}

/** Kebab: the "more" control on rows. Horizontal in lists, vertical on agenda rows. */
export function Kebab({ label, vertical, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; vertical?: boolean }) {
  const I = vertical ? MoreVertical : MoreHorizontal;
  return <IconButton label={label} size={28} round className={className} {...rest}><I size={ICON.md} strokeWidth={ICON.stroke} aria-hidden /></IconButton>;
}
