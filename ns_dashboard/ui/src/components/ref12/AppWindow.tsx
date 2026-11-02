import type { CSSProperties, ReactNode } from "react";
import { cx } from "../../ui/kit";

/** AppWindow: depth layer 1, the rounded app frame on the canvas (radius 2xl, shadow-frame).
 *  `glass` (the app shell) makes it translucent: the app colour at --ds-window-alpha with a backdrop blur and a 1 px
 *  inner border (.ds-glass in styles.css); otherwise it is the opaque bg-app frame of the recreation.
 *  Not a shell: it has no rail or header of its own. Overflow stays visible so floating cards may cross its edge. */
export function AppWindow({ children, className, style, glass }: { children: ReactNode; className?: string; style?: CSSProperties; glass?: boolean }) {
  return <div className={cx("ds-root relative rounded-ds-2xl", glass ? "ds-glass" : "bg-ds-app shadow-ds-frame", className)} style={style}>{children}</div>;
}
