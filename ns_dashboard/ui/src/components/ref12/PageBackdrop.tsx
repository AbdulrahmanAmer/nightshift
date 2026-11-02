import { cx } from "../../ui/kit";

/** PageBackdrop: depth layer 0. The canvas colour with faint white hairline curves, a few small dots and two
 *  rings, placed as in the reference (positions in % of a 1440x1080 frame). Decorative only. */
export function PageBackdrop({ className }: { className?: string }) {
  return (
    <div aria-hidden className={cx("pointer-events-none absolute inset-0 overflow-hidden bg-ds-canvas", className)}>
      <svg className="absolute inset-0 size-full" viewBox="0 0 1440 1080" preserveAspectRatio="none" fill="none">
        <g style={{ stroke: "var(--ds-backdrop-line)" }} strokeWidth="1.5">
          <path d="M1250 40 C1320 10 1400 -10 1460 -30" />
          <path d="M-20 300 C60 270 140 250 200 236" />
          <path d="M-20 720 C40 690 120 700 150 760 C170 800 120 860 60 880" />
          <path d="M340 1100 C420 1020 520 1030 600 1040 C700 1050 780 1060 840 1090" />
          <path d="M1400 560 C1460 600 1460 680 1400 700 C1360 712 1360 640 1420 620" />
          <path d="M1380 980 C1420 940 1460 930 1480 935" />
        </g>
      </svg>
      <span className="absolute size-2.5 rounded-full bg-ds-success" style={{ left: "2.1%", top: "76.3%" }} />
      <span className="absolute size-3 rounded-full bg-ds-accent" style={{ left: "96.3%", top: "66.4%" }} />
      <span className="absolute size-1.5 rounded-full bg-ds-success" style={{ left: "91.3%", top: "2.8%" }} />
      <span className="absolute size-9 rounded-full border-[1.5px] border-ds-text/40" style={{ left: "5.3%", top: "93.6%" }} />
      <span className="absolute size-7 rounded-full bg-ds-text/70" style={{ left: "89.8%", top: "94.6%" }} />
    </div>
  );
}
