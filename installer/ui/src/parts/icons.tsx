// Icons that the shared ref12 registry does not have yet (lucide, same 18 px / 1.75 stroke). Everything else comes from
// the registry through <Icon>. SetupIcon takes both name sets so callers do not care which one holds the glyph.
import { CircleX, MemoryStick, Package, ShieldAlert, SquareTerminal, TriangleAlert, Wifi, WifiOff, type LucideIcon } from "lucide-react";
import { Icon, type IconName } from "@ref12";

const local = { error: CircleX, warning: TriangleAlert, memory: MemoryStick, network: Wifi, offline: WifiOff, shield: ShieldAlert, package: Package, terminal: SquareTerminal } satisfies Record<string, LucideIcon>;
export type LocalIconName = keyof typeof local;
export type SetupIconName = IconName | LocalIconName;

export function SetupIcon({ name, size = 18, strokeWidth = 1.75, className, label }: { name: SetupIconName; size?: number; strokeWidth?: number; className?: string; label?: string }) {
  if (name in local) {
    const Cmp = local[name as LocalIconName];
    return <Cmp size={size} strokeWidth={strokeWidth} className={className} aria-hidden={label ? undefined : true} aria-label={label} role={label ? "img" : undefined} />;
  }
  return <Icon name={name as IconName} size={size} strokeWidth={strokeWidth} className={className} label={label} />;
}
