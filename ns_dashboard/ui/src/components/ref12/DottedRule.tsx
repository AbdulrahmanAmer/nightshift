import { cx } from "../../ui/kit";

/** DottedRule (Planify): the 2 px dotted separator that runs from an agenda time to the card edge. */
export function DottedRule({ className }: { className?: string }) {
  return <span aria-hidden className={cx("block h-0 flex-1 border-t-2 border-dotted border-ds-border", className)} />;
}
