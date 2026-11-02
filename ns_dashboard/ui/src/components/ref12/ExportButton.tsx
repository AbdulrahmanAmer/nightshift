import { ArrowDown } from "lucide-react";
import type { ButtonHTMLAttributes } from "react";
import { cx } from "../../ui/kit";
import { KbdChip } from "./KbdChip";
import { ICON } from "./tokens";

/** ExportButton: soft tray button (36 px, 10 px radius, tint-2) with a down arrow, label and a trailing KbdChip. */
export function ExportButton({ label, chip, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; chip?: string }) {
  return (
    <button type="button" {...rest} className={cx("r12-focus inline-flex h-9 shrink-0 items-center gap-2 rounded-r12-control bg-r12-tint-2 pr-[7px] pl-3 text-[13px] text-r12-text-2 transition-[filter,color] duration-150 hover:text-r12-ink hover:brightness-[0.97]", className)}>
      <ArrowDown size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />
      <span>{label}</span>
      {chip && <KbdChip>{chip}</KbdChip>}
    </button>
  );
}
