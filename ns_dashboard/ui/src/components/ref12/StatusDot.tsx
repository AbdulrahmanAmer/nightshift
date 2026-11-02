import { cx } from "../../ui/kit";

const T = { online: "bg-ds-online", attention: "bg-ds-attention", accent: "bg-ds-accent", danger: "bg-ds-danger", success: "bg-ds-success" } as const;
export type DotTone = keyof typeof T;

/** StatusDot (Planify): 7 px dot. `ring` adds a halo in the surface colour so it can overlap icons.
 *  With a `label` it is announced as a status; without, it is decorative. */
export function StatusDot({ tone = "online", ring, label, size = 7, className }: { tone?: DotTone; ring?: boolean; label?: string; size?: 5 | 7 | 8; className?: string }) {
  return (
    <span role={label ? "status" : undefined} aria-label={label} aria-hidden={label ? undefined : true}
      className={cx("inline-block shrink-0 rounded-full", T[tone], ring && "shadow-[0_0_0_2px_var(--ds-bg-surface)]", className)}
      style={{ width: size, height: size }} />
  );
}
