// Window controls for the frameless WebView2 host (docs/design/installer-ui.md section 6): the owner-approved "notch" design.
// Two pills hang from the top edge in the rail's fill: the brand mark top left, minimize / maximize / close top right,
// and an invisible 36 px strip across the top is the drag region. Same geometry and visuals as the dashboard's
// shell/TitleBar.tsx, which cannot be imported here (it needs the dashboard's app context). Renders nothing in a browser.
import { useEffect, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import { Icon, type IconName } from "@ref12";
import { Mark } from "@dash/ui/Mark";
import { host } from "../host";
import { shell } from "../copy";

const H = 36;
const CAP = Math.round(H * 1.2); // the rail cap is 100 x 120; turned on its side it is 1.2 x as long as it is deep

/** The notch outline: the rail cap (M0 0 C0 36 22 48 50 58 C80 68 100 84 100 120) with x and y swapped, mirrored at the right end. */
function notchPath(w: number) {
  const L = CAP, p = (x: number, y: number) => `${x.toFixed(1)} ${y.toFixed(1)}`;
  const left = `M0 0 C${p(0.3 * L, 0)} ${p(0.4 * L, 0.22 * H)} ${p(0.4833 * L, 0.5 * H)} C${p(0.5667 * L, 0.8 * H)} ${p(0.7 * L, H)} ${p(L, H)}`;
  const right = `L${p(w - L, H)} C${p(w - 0.7 * L, H)} ${p(w - 0.5667 * L, 0.8 * H)} ${p(w - 0.4833 * L, 0.5 * H)} C${p(w - 0.4 * L, 0.22 * H)} ${p(w - 0.3 * L, 0)} ${p(w, 0)} Z`;
  return left + " " + right;
}

function Notch({ width, side, children, label }: { width: number; side: "left" | "right"; children: ReactNode; label: string }) {
  return (
    <div role="group" aria-label={label} className="ds-notch fixed top-0 z-50" style={{ width, height: H, [side]: 12 } as CSSProperties}>
      <div aria-hidden className="absolute inset-0" style={{ background: "var(--rail-bg)", clipPath: `path("${notchPath(width)}")` }} />
      <div className="ds-nodrag relative flex h-full items-center justify-center gap-1 pb-0.5">{children}</div>
    </div>
  );
}

function NotchButton({ icon, label, onClick, danger }: { icon: IconName; label: string; onClick: () => void; danger?: boolean }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick}
      className={`grid size-7 place-items-center rounded-full text-ds-on-accent/85 outline-none transition-colors duration-[120ms] hover:text-ds-on-accent focus-visible:shadow-[0_0_0_2px_var(--ds-text-on-accent)] ${danger ? "hover:bg-ds-danger" : "hover:bg-ds-on-accent/15"}`}>
      <Icon name={icon} size={14} strokeWidth={2} />
    </button>
  );
}

export function WindowNotches({ onCloseRequest }: { onCloseRequest: () => void }) {
  const [maximized, setMaximized] = useState(false);
  const available = host.available();

  useEffect(() => {
    if (!available) return;
    const off = host.on((e) => {
      if (e.event === "window-state") setMaximized(e.state === "maximized");
      if (e.event === "close-requested") onCloseRequest();
    });
    host.send({ cmd: "ready" });
    return off;
  }, [available, onCloseRequest]);

  if (!available) return null;
  const down = (e: PointerEvent<HTMLElement>) => { if (e.button === 0) host.send({ cmd: "drag-start" }); };
  return (
    <>
      <div aria-hidden className="ds-drag fixed inset-x-0 top-0 z-40 h-9" onPointerDown={down} onDoubleClick={() => host.send({ cmd: "toggle-maximize" })} />
      <Notch side="left" width={CAP + 36} label={shell.brandGroup}>
        <span className="ds-drag grid size-7 place-items-center" onPointerDown={down}><Mark size={18} tone="white" /></span>
      </Notch>
      <Notch side="right" width={CAP + 92} label={shell.windowGroup}>
        <NotchButton icon="winMinimize" label={shell.minimize} onClick={() => host.send({ cmd: "minimize" })} />
        <NotchButton icon={maximized ? "winRestore" : "winMaximize"} label={maximized ? shell.restore : shell.maximize} onClick={() => host.send({ cmd: "toggle-maximize" })} />
        <NotchButton icon="winClose" label={shell.close} danger onClick={onCloseRequest} />
      </Notch>
    </>
  );
}
