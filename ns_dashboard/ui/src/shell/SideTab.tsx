import { useEffect, useState, type CSSProperties } from "react";
import { BookOpen, Clapperboard, LineChart, MessageSquare, Moon, Play, Power, Settings, Sun, Tv } from "lucide-react";
import { useApp } from "../lib/app";
import { api } from "../lib/api";
import { cx } from "../ui/kit";
import { Mark } from "../ui/Mark";

export const NAV = [
  { id: "queue", href: "#/", label: "Queue", icon: Play },
  { id: "create", href: "#/create", label: "Create", icon: Clapperboard },
  { id: "library", href: "#/library", label: "Library", icon: BookOpen },
  { id: "assistant", href: "#/assistant", label: "Assistant", icon: MessageSquare },
  { id: "channels", href: "#/channels", label: "Channels", icon: Tv },
  { id: "results", href: "#/results", label: "Results", icon: LineChart },
  { id: "settings", href: "#/settings", label: "Settings", icon: Settings },
];

/* Geometry of the owner's shape (docs/design-tool/sidetab.svg, scaled 1.2 so the tab is 96 px wide).
   Only the straight middle stretches with the height; the slant (about 40 degrees) and both corner radii are fixed. */
const W = 96;
const BRAND_TOP = 54;
const ITEMS_TOP = 96;
const TOOLS_BOTTOM = 54;
const TOOL = 32;

export const tabPath = (h: number) =>
  `M0 0 L60 50 C84 69 96 98 96 144 L96 ${h - 141.6} C96 ${h - 96} 84 ${h - 69.6} 62.4 ${h - 50.4} L0 ${h} Z`;

/* About 80% of the viewport, 560 to 760 px. The spec asked for a 520 px floor, but seven items plus the
   brand and two tools need roughly 560 px at the minimum pitch. */
const heightFor = (vh: number) => Math.round(Math.min(760, Math.max(560, vh * 0.8)));

function useTabHeight() {
  const [h, setH] = useState(() => heightFor(window.innerHeight));
  useEffect(() => {
    const on = () => setH(heightFor(window.innerHeight));
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  return h;
}

export function SideTab() {
  const { route, status, theme, setTheme, confirm, toast, refresh } = useApp();
  const h = useTabHeight();
  const pending = status?.outbox?.pending ?? 0;
  const idx = NAV.findIndex((n) => n.id === route.name);
  const pitch = Math.max(44, Math.min(56, Math.floor((h - ITEMS_TOP - TOOLS_BOTTOM - TOOL * 2 - 6) / NAV.length)));

  const quit = async () => {
    const ok = await confirm({ title: "Quit Nightshift?", body: "Stops the engine and frees the graphics card.", ok: "Quit", danger: true });
    if (ok !== true) return;
    try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Shutting down. You can close this tab", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  const vars = { "--h": `${h}px`, "--pitch": `${pitch}px`, "--i": Math.max(idx, 0), "--tab-w": `${W}px`, "--items-top": `${ITEMS_TOP}px` } as CSSProperties;

  return (
    <nav aria-label="Main" className="sidetab" style={vars}>
      <svg className="sidetab-shape" width={W} height={h} viewBox={`0 0 ${W} ${h}`} aria-hidden>
        <defs>
          <linearGradient id="ns-tab-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#4b3fa8" />
            <stop offset="1" stopColor="#2d2366" />
          </linearGradient>
        </defs>
        <path d={tabPath(h)} fill="url(#ns-tab-fill)" />
      </svg>
      <a href="#/" className="sidetab-brand" aria-label="Nightshift, Queue" style={{ top: BRAND_TOP }}><Mark size={28} tone="white" /></a>

      <div className="sidetab-items">
        <span className="sidetab-cell" data-off={idx < 0 || undefined} aria-hidden><span className="sidetab-square" /></span>
        {NAV.map((n, i) => {
          const on = i === idx; const I = n.icon; const badge = n.id === "queue" && pending > 0;
          return (
            <a key={n.id} href={n.href} aria-current={on ? "page" : undefined} aria-label={badge ? `${n.label}, ${pending} ready` : n.label} className={cx("sidetab-item", on && "is-on")}>
              <span className="sidetab-ico">
                <I size={22} strokeWidth={1.75} aria-hidden />
                {badge && <span key={pending} className="sidetab-badge num" aria-hidden>{pending}</span>}
              </span>
              <span className="sidetab-tip" aria-hidden>{n.label}</span>
            </a>
          );
        })}
      </div>

      <div className="sidetab-tools">
        <button type="button" className="sidetab-item sidetab-tool" aria-label={themeLabel} onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          <span className="sidetab-ico">{theme === "dark" ? <Sun size={20} strokeWidth={1.75} aria-hidden /> : <Moon size={20} strokeWidth={1.75} aria-hidden />}</span>
          <span className="sidetab-tip" aria-hidden>{theme === "dark" ? "Light theme" : "Dark theme"}</span>
        </button>
        <button type="button" className="sidetab-item sidetab-tool" aria-label="Quit Nightshift" onClick={quit}>
          <span className="sidetab-ico"><Power size={20} strokeWidth={1.75} aria-hidden /></span>
          <span className="sidetab-tip" aria-hidden>Quit</span>
        </button>
      </div>
    </nav>
  );
}
