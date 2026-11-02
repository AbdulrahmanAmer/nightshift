import { cx } from "../../ui/kit";

export interface Person { name: string; src?: string }
export type AvatarSize = "xs" | "sm" | "md";

/* Planify's five initials fills (light pastel + deep ink), picked by a stable hash of the name. */
const FILLS: [string, string][] = [["#ffd7b8", "#b9603a"], ["#c9e7ff", "#2f6d9e"], ["#ffd6e4", "#a53f66"], ["#d8f1e5", "#2f7d5c"], ["#e5ddff", "#5b47b8"]];
const fillFor = (name: string) => { let h = 0; for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return FILLS[h % FILLS.length]!; };
const initials = (name: string, n: number) => name.split(/\s+/).filter(Boolean).map((p) => p[0]!).slice(0, n).join("").toUpperCase();

export const AVATAR_SIZE: Record<AvatarSize, string> = { xs: "size-[18px] text-[8px]", sm: "size-6 text-[9px]", md: "size-8 text-ds-2xs" };

/** Avatar (Planify): 18 / 24 / 32 px photo with an initials fallback coloured from the name. */
export function Avatar({ name, src, size = "sm", className }: Person & { size?: AvatarSize; className?: string }) {
  const [bg, fg] = fillFor(name);
  return (
    <span title={name} className={cx("inline-grid shrink-0 place-items-center overflow-hidden rounded-full leading-none font-bold", AVATAR_SIZE[size], className)} style={src ? undefined : { background: bg, color: fg }}>
      {src ? <img src={src} alt={name} className="size-full object-cover" /> : <><span aria-hidden>{initials(name, size === "md" ? 2 : 1)}</span><span className="sr-only">{name}</span></>}
    </span>
  );
}
