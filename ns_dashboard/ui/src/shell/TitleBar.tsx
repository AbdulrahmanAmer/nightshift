/* Window controls for the frameless WebView2 app host (docs/design/installer-ui.md §6). Not a full-width bar: two
   notches that hang from the top edge in the rail's fill (--rail-bg, per theme) and shadow, their ends drawn with the rail's wave-cap curve
   turned 90 degrees (flat against the top, shoulders curving down into the pill). Top right: minimize,
   maximize/restore and close. Top left: the brand mark. An invisible 36 px strip across the top is the drag region
   (-webkit-app-region: drag, and drag-start is also posted on a primary press). Renders nothing in a normal browser.
   Close asks first: "Close window" closes only the window (the engine keeps running), "Quit Nightshift" stops the
   engine, then closes. The host's close-requested event (Alt+F4, taskbar) asks the same question. */
import { useCallback, useEffect, useState, type PointerEvent, type ReactNode } from "react";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { host } from "../lib/host";
import { Icon, type IconName } from "../components/ref12";
import { Mark } from "../ui/Mark";

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
    <div role="group" aria-label={label} className="ds-notch fixed top-0 z-50" style={{ width, height: H, [side]: 12 } as React.CSSProperties}>
      <div aria-hidden className="absolute inset-0" style={{ background: "var(--rail-bg)", clipPath: `path("${notchPath(width)}")` }} />
      <div className="ds-nodrag relative flex h-full items-center justify-center gap-1 pb-0.5">{children}</div>
    </div>
  );
}

function NotchButton({ icon, label, onClick, danger }: { icon: IconName; label: string; onClick: () => void; danger?: boolean }) {
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick}
      className={`grid size-7 place-items-center rounded-full text-white/85 outline-none transition-colors duration-[120ms] hover:text-white focus-visible:shadow-[0_0_0_2px_#fff] ${danger ? "hover:bg-ds-danger" : "hover:bg-white/15"}`}>
      <Icon name={icon} size={14} strokeWidth={2} />
    </button>
  );
}

export function TitleBar() {
  const { confirm, toast } = useApp();
  const [maximized, setMaximized] = useState(false);
  const available = host.available();

  const requestClose = useCallback(async () => {
    host.send({ cmd: "close-ack" });
    const v = await confirm({ title: "Close Nightshift?", body: "Close the window and the engine keeps working tonight. Quit stops the engine and frees the graphics card.", ok: "Close window", alt: "Quit Nightshift" });
    if (v === true) host.send({ cmd: "close" });
    else if (v === "alt") {
      try { await api.post("/engine/shutdown", { grace_s: 30 }); } catch (e: any) { toast(e.message, "bad"); return; }
      host.send({ cmd: "close" });
    }
  }, [confirm, toast]);

  useEffect(() => {
    if (!available) return;
    const off = host.on((e) => {
      if (e.event === "window-state") setMaximized(e.state === "maximized");
      if (e.event === "close-requested") requestClose();
    });
    host.send({ cmd: "ready" });
    return off;
  }, [available, requestClose]);

  if (!available) return null;
  const down = (e: PointerEvent<HTMLDivElement>) => { if (e.button === 0) host.send({ cmd: "drag-start" }); };
  return (
    <>
      <div aria-hidden className="ds-drag fixed inset-x-0 top-0 z-40 h-9" onPointerDown={down} onDoubleClick={() => host.send({ cmd: "toggle-maximize" })} />
      <Notch side="left" width={CAP + 36} label="Nightshift">
        <span className="ds-drag grid size-7 place-items-center" onPointerDown={(e) => { if (e.button === 0) host.send({ cmd: "drag-start" }); }}><Mark size={18} tone="white" /></span>
      </Notch>
      <Notch side="right" width={CAP + 92} label="Window">
        <NotchButton icon="winMinimize" label="Minimize" onClick={() => host.send({ cmd: "minimize" })} />
        <NotchButton icon={maximized ? "winRestore" : "winMaximize"} label={maximized ? "Restore" : "Maximize"} onClick={() => host.send({ cmd: "toggle-maximize" })} />
        <NotchButton icon="winClose" label="Close" danger onClick={requestClose} />
      </Notch>
    </>
  );
}
