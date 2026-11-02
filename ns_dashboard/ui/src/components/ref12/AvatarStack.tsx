import { Plus } from "lucide-react";
import { cx } from "../../ui/kit";

export interface AvatarData { name: string; src?: string; hue?: number }

const initials = (n: string, k = 2) => n.split(/\s+/).filter(Boolean).slice(0, k).map((w) => w[0]!.toUpperCase()).join("");
/** Stable pastel hue from the name when no hue is given. */
const hueOf = (n: string) => [...n].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);

/** Avatar: round photo or initials on a soft tint. Sizes 20 / 24 / 28 / 32. */
export function Avatar({ name, src, hue, size = 24, className, ring = true }: AvatarData & { size?: 20 | 24 | 28 | 32; className?: string; ring?: boolean }) {
  const h = hue ?? hueOf(name);
  return (
    <span title={name} className={cx("inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full font-semibold select-none", ring && "ring-2 ring-r12-card", className)}
      style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.38)), background: src ? undefined : `hsl(${h} 70% 86%)`, color: `hsl(${h} 45% 28%)` }}>
      {src ? <img src={src} alt="" className="size-full object-cover" /> : <span aria-hidden>{initials(name, size < 28 ? 1 : 2)}</span>}
      <span className="sr-only">{name}</span>
    </span>
  );
}

/** AvatarStack: overlapping avatars (-8 px), then an optional accent "+" or "+N" bubble.
 *  Reference: three 24 px faces plus a purple plus next to "Today tasks". */
export function AvatarStack({ people, max = 3, size = 24, more, onMore, moreLabel, className }: {
  people: AvatarData[]; max?: number; size?: 20 | 24 | 28 | 32; more?: number; onMore?: () => void; moreLabel?: string; className?: string;
}) {
  const shown = people.slice(0, max); const extra = more ?? Math.max(0, people.length - max);
  const bubble = "inline-flex shrink-0 items-center justify-center rounded-full bg-r12-accent text-r12-on-accent ring-2 ring-r12-card font-mono text-[11px]";
  return (
    <span className={cx("inline-flex items-center", className)}>
      {shown.map((p, i) => <Avatar key={p.name + i} {...p} size={size} className={i ? "-ml-2" : ""} />)}
      {onMore ? (
        <button type="button" onClick={onMore} aria-label={moreLabel} title={moreLabel} className={cx(bubble, "r12-focus -ml-2")} style={{ width: size, height: size }}>
          {extra > 0 ? `+${extra}` : <Plus size={12} strokeWidth={2.25} aria-hidden />}
        </button>
      ) : extra > 0 ? <span className={cx(bubble, "-ml-2")} style={{ width: size, height: size }}>+{extra}</span> : null}
    </span>
  );
}
