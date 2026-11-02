import { cx } from "../../ui/kit";
import { Avatar, AVATAR_SIZE, type AvatarSize, type Person } from "./Avatar";
import { Icon } from "./Icon";

/** AvatarGroup (Planify): avatars overlapping 7 px with a 2 px surface ring, a "+N" chip for the rest,
 *  and an optional accent "+" add button (scaled 0.8, as in "Today tasks"). */
export function AvatarGroup({ people, max = 3, size = "sm", onAdd, addLabel, className }: { people: Person[]; max?: number; size?: AvatarSize; onAdd?: () => void; addLabel?: string; className?: string }) {
  const shown = people.slice(0, max); const extra = people.length - shown.length;
  const ring = "shadow-[0_0_0_2px_var(--ds-bg-surface)]";
  return (
    <span className={cx("inline-flex items-center", className)}>
      {shown.map((p, i) => <Avatar key={p.name} {...p} size={size} className={cx(ring, i > 0 && "-ml-[7px]")} />)}
      {extra > 0 && <span className={cx("-ml-[7px] inline-grid place-items-center rounded-full bg-ds-sunken leading-none font-bold text-ds-text-2", AVATAR_SIZE[size], ring)}>+{extra}</span>}
      {onAdd && (
        <button type="button" onClick={onAdd} aria-label={addLabel} title={addLabel}
          className={cx("ds-focus -ml-1 inline-grid scale-[0.8] place-items-center rounded-full bg-ds-accent text-ds-on-accent", AVATAR_SIZE[size], ring)}>
          <Icon name="plus" size={10} strokeWidth={2.5} />
        </button>
      )}
    </span>
  );
}
