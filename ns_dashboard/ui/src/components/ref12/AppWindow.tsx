import type { CSSProperties, ReactNode } from "react";
import { cx } from "../../ui/kit";

/** AppWindow: depth layer 1, the rounded app frame on the canvas (bg-app, radius 2xl, shadow-frame).
 *  Not a shell: it has no rail or header of its own (the rail is SideTab, PR #25). Overflow stays visible so
 *  floating cards may cross its edge, as the third feature tile does in the reference. */
export function AppWindow({ children, className, style }: { children: ReactNode; className?: string; style?: CSSProperties }) {
  return <div className={cx("ds-root relative rounded-ds-2xl bg-ds-app shadow-ds-frame", className)} style={style}>{children}</div>;
}
