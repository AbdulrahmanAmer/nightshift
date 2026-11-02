import { Plus } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";
import { cx } from "../../ui/kit";

/** PlusTile: the 24/28 px accent square with a white plus used inside dashed adders. */
export function PlusTile({ size = 28 }: { size?: 24 | 28 }) {
  return <span aria-hidden className="inline-flex shrink-0 items-center justify-center rounded-[8px] bg-r12-accent text-r12-on-accent" style={{ width: size, height: size }}><Plus size={size === 28 ? 16 : 14} strokeWidth={2} /></span>;
}

/** DashedAddCard: the empty square tile left of the feature cards. 24 px radius, 1.5 px dashed border,
 *  tint fill, centred PlusTile. Square by default (aspect 1), fills its grid cell. */
export function DashedAddCard({ label, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button type="button" aria-label={label} title={label} {...rest}
      className={cx("r12-focus group flex aspect-square w-full min-w-0 items-center justify-center rounded-r12-tile border-[1.5px] border-dashed border-r12-dash bg-r12-tint transition-colors duration-150 hover:border-r12-accent", className)}>
      <span className="transition-transform duration-150 group-active:scale-95"><PlusTile /></span>
    </button>
  );
}

/** DashedAddButton: the full-width "Add new assignment" bar. 56 px tall, 16 px radius, same dashed treatment. */
export function DashedAddButton({ label, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button type="button" {...rest}
      className={cx("r12-focus flex h-14 w-full items-center justify-center gap-3 rounded-r12-row border-[1.5px] border-dashed border-r12-dash bg-r12-tint text-[13px] font-medium text-r12-ink transition-colors duration-150 hover:border-r12-accent", className)}>
      <PlusTile size={24} />{label}
    </button>
  );
}
