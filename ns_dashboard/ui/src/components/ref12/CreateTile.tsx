import { cx } from "../../ui/kit";
import { Icon } from "./Icon";

/** CreateTile (Planify): dashed creation affordance with an accent plus that glows.
 *  square = empty board slot (min 160 px tall, radius xl, label is the accessible name);
 *  row = "Add new assignment" bar (56 px, radius lg, visible label). */
export function CreateTile({ label, layout = "square", onClick, className }: { label: string; layout?: "square" | "row"; onClick?: () => void; className?: string }) {
  const sq = layout === "square";
  return (
    <button type="button" onClick={onClick} aria-label={sq ? label : undefined} title={sq ? label : undefined}
      className={cx(
        "ds-focus flex w-full items-center justify-center gap-3 border-[1.5px] border-dashed border-ds-dashed bg-ds-accent-subtle text-ds-xs font-medium text-ds-text transition-[background-color,border-color] duration-[120ms] ease-ds-out hover:border-ds-accent hover:bg-ds-accent-soft",
        sq ? "h-full min-h-40 rounded-ds-xl" : "h-14 rounded-ds-lg", className,
      )}>
      <span aria-hidden className={cx("grid place-items-center bg-ds-accent text-ds-on-accent shadow-ds-accent", sq ? "size-[26px] rounded-ds-sm" : "size-[22px] rounded-ds-xs")}>
        <Icon name="plus" size={14} strokeWidth={2.4} />
      </span>
      {!sq && <span>{label}</span>}
    </button>
  );
}
