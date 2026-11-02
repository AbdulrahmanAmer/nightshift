import type { CSSProperties, ReactNode } from "react";
import { cx } from "../../ui/kit";

/** AppWindow: depth layer 1 of the ref12 recreation, the rounded app frame on the canvas (bg-app, radius 2xl,
 *  shadow-frame). The app shell no longer uses it: pages sit straight on the canvas. Overflow stays visible so floating
 *  cards may cross its edge, as the third feature tile does in the reference. */
export function AppWindow({ children, className, style }: { children: ReactNode; className?: string; style?: CSSProperties }) {
  return <div className={cx("ds-root relative rounded-ds-2xl bg-ds-app shadow-ds-frame", className)} style={style}>{children}</div>;
}
