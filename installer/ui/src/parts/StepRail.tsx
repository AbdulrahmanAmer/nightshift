// The step rail (docs/design/installer-ui.md section 5.3): the dashboard's SideTab wave, showing progress instead of navigation.
// Same markup and CSS classes as shell/SideTab.tsx (.sidetab*, in styles.css); the geometry comes from SideTab's exports.
import { useEffect, useState, type CSSProperties } from "react";
import { cx } from "@dash/ui/kit";
import { CAP_PATH, RAIL_W, layoutFor } from "@dash/shell/SideTab";
import { Mark } from "@dash/ui/Mark";
import { SetupIcon, type SetupIconName } from "./icons";
import { shell } from "../copy";

export interface RailStep { id: string; label: string; icon: SetupIconName; state: "done" | "current" | "upcoming" | "failed"; onSelect?: () => void }

const CAP = RAIL_W * 1.2;
const capUri = (flip: boolean) => {
  const g = flip ? `<g transform="translate(0 120) scale(1 -1)"><path d="${CAP_PATH}"/></g>` : `<path d="${CAP_PATH}"/>`;
  return `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 120" preserveAspectRatio="none">${g}</svg>`)}")`;
};
const CAP_START = capUri(false);
const CAP_END = capUri(true);

/** SideTab's useRailLayout is not exported; this is the same hook; the mark sits in the brand slot at the top. */
function useRailLayout() {
  const [l, setL] = useState(() => layoutFor(window.innerHeight, true));
  useEffect(() => {
    const on = () => setL((p) => { const n = layoutFor(window.innerHeight, true); return n.gap === p.gap ? p : n; });
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  return l;
}

export function StepRail({ steps, label = shell.railLabel, className }: { steps: RailStep[]; label?: string; className?: string }) {
  const l = useRailLayout();
  const idx = steps.findIndex((s) => s.state === "current" || s.state === "failed");
  const h = Math.max(Math.round(2 * CAP * 0.58 + (steps.length + 1) * 40 + steps.length * l.gap), Math.ceil(2 * CAP) + 24);
  const vars = { "--h": `${h}px`, "--gap": `${l.gap}px`, "--i": Math.max(idx, 0), "--cap-start": CAP_START, "--cap-end": CAP_END, "--cap": `${CAP}px` } as CSSProperties;
  const stateWord = { done: "done", current: "current step", upcoming: "not started", failed: "stopped with an error" } as const;
  return (
    <nav aria-label={label} className={cx("sidetab", steps.length < 4 && "ns-rail-short", className)} data-brand="on" style={vars}>
      <div className="sidetab-shape" aria-hidden><div className="sidetab-fill" /></div>
      <div className="sidetab-list">
        <span className="sidetab-item sidetab-brand" role="img" aria-label={shell.brandGroup}><Mark size={32} tone="white" /></span>
        <div className="sidetab-items">
          <span className="sidetab-cell" data-off={idx < 0 || undefined} aria-hidden><span className="sidetab-square" /></span>
          {steps.map((s) => {
            const on = s.state === "current" || s.state === "failed";
            const cls = cx("sidetab-item", on && "is-on");
            const inner = (
              <>
                <SetupIcon name={s.icon} size={18} />
                {s.state === "done" && <span className="sidetab-dot" data-tone="success" aria-hidden />}
                {s.state === "failed" && <span className="sidetab-dot" data-tone="attention" aria-hidden />}
                <span className="sidetab-tip" aria-hidden>{s.label}</span>
              </>
            );
            const name = `${s.label}, ${stateWord[s.state]}`;
            if (s.onSelect && !on) return <button key={s.id} type="button" className={cls} aria-label={name} onClick={s.onSelect}>{inner}</button>;
            return <span key={s.id} className={cls} aria-label={name} aria-current={on ? "step" : undefined} role="img">{inner}</span>;
          })}
        </div>
      </div>
    </nav>
  );
}
