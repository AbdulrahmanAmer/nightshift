/* App shell on the ref12 depth layers: canvas (the owner's swirl image, see Canvas) -> SideTab rail -> the rounded
   AppWindow holding the TopBar and the page. Toasts, the confirm dialog and the Ask drawer sit above it. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../lib/app";
import { fmtWhen } from "../lib/format";
import type { Status } from "../lib/types";
import { cx } from "../ui/kit";
import { Chat } from "../pages/Assistant";
import { Mark } from "../ui/Mark";
import { AppWindow, Button, Card, DigitalClock, Field, IconButton, Input, SearchInput, SegmentedControl, StatusChip, type SegmentOption } from "../components/ref12";
import { SideTab } from "./SideTab";
export { Mark };

export function engineText(s: Status | null) {
  const e = s?.engine; if (!e) return { text: "Connecting", tone: "muted" as const };
  const clean = (t?: string) => (t || "").replace(/\.{3}|…/g, "").trim();
  if (e.state === "loading") {
    const svc = e.service && s?.services?.[e.service]; const gb = e.vram_gb ?? (svc && svc.vram_mb ? Math.round(svc.vram_mb / 1024) : null);
    return { text: `Loading model${gb ? ` ${gb} GB` : ""}${e.elapsed_s != null ? ` · ${e.elapsed_s} s` : ""}`, tone: "accent" as const };
  }
  if (e.state === "working") return { text: `Working: ${clean(e.detail) || "Working"}${e.step && e.steps ? `, step ${e.step} of ${e.steps}` : ""}`, tone: "accent" as const, live: true };
  if (e.state === "ready") return { text: "Ready", tone: "ok" as const };
  if (e.state === "starting") return { text: "Starting", tone: "accent" as const };
  if (e.state === "error") return { text: clean(e.detail) || "Engine problem", tone: "bad" as const };
  return { text: clean(e.detail) === "Engine stopped" ? "Engine stopped" : "Idle", tone: "muted" as const };
}
const DOT = { muted: "muted", accent: "accent", ok: "online", bad: "danger" } as const;

const TITLES: Record<string, [string, string]> = {
  queue: ["Queue", "Watch, approve, then post"],
  create: ["Create", "Start new work without waiting for tonight"],
  library: ["Library", "Everything Nightshift knows and can reuse"],
  assistant: ["Assistant", "Run Nightshift by talking to it"],
  channels: ["Channels", "Set up and adjust each YouTube channel"],
  results: ["Results", "What works, and what Nightshift changes next"],
  settings: ["Settings", "Keep the machine running"],
};
type Theme = "light" | "dark" | "soft";
const THEMES: SegmentOption<Theme>[] = [
  { value: "light", icon: "sun", ariaLabel: "Light theme" },
  { value: "dark", icon: "moon", ariaLabel: "Dark theme" },
  { value: "soft", icon: "blend", ariaLabel: "Soft theme" },
];

/** Search field that hands what was typed to the Assistant page. */
function CommandSearch({ className }: { className?: string }) {
  const { route, go, ask } = useApp();
  const [q, setQ] = useState("");
  return (
    <form role="search" className={className} onSubmit={(e) => { e.preventDefault(); const t = q.trim(); if (!t) return; setQ(""); ask({ page: route.name }, t, false); go("#/assistant"); }}>
      <SearchInput label="Ask or search" placeholder="Ask Nightshift or search" value={q} onChange={(e) => setQ(e.target.value)} shortcut="Enter" />
    </form>
  );
}

function TopBar() {
  const { status, demo, ask, route, theme, setTheme, go } = useApp();
  const eng = engineText(status);
  const [title, sub] = TITLES[route.name] || TITLES.queue!;
  const pending = status?.outbox?.pending ?? 0;
  const alert = !!status?.last_error;
  const bell = alert ? "Notifications, the last run stopped" : pending ? `Notifications, ${pending} ready to review` : "Notifications";
  return (
    <header className="@container">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-3 @min-[1100px]:flex-nowrap">
        <div className="mr-auto min-w-0 @min-[1100px]:mr-0 @min-[1100px]:w-[200px] @min-[1100px]:shrink-0">
          <h1 className="truncate text-ds-xl leading-ds-tight font-semibold tracking-[-0.01em]">{title}</h1>
          <p className="truncate text-ds-2xs text-ds-muted">{demo ? "Sample data: no engine answered" : sub}</p>
        </div>
        <CommandSearch className="order-last basis-full @min-[760px]:order-none @min-[760px]:basis-auto @min-[760px]:flex-[1_1_220px] @min-[1100px]:max-w-[340px]" />
        <div className="hidden min-w-0 items-center gap-2 @min-[760px]:flex @min-[1100px]:ml-auto">
          <StatusChip tone={DOT[eng.tone]} live title="What the engine is doing">{eng.text}</StatusChip>
          <span className="hidden @min-[960px]:contents"><StatusChip tone="muted" title="Next automatic run">{status?.next_run ? <>Next run <span className="ds-num">{fmtWhen(status.next_run)}</span></> : "Automatic runs off"}</StatusChip></span>
        </div>
        <span className="hidden @min-[1180px]:contents"><DigitalClock variant="inline" /></span>
        <span className="hidden @min-[760px]:contents"><SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEMES} /></span>
        <div className="flex items-center gap-1">
          <IconButton icon="bell" label={bell} dot={alert || pending > 0} onClick={() => go("#/")} />
          <IconButton icon="settings" label="Settings" onClick={() => go("#/settings")} aria-current={route.name === "settings" ? "page" : undefined} />
        </div>
        <Button leadingIcon="chat" onClick={() => ask({ page: route.name })} aria-label="Ask Nightshift"><span className="hidden @min-[1000px]:inline">Ask Nightshift</span></Button>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 @min-[760px]:hidden">
        <StatusChip tone={DOT[eng.tone]} live>{eng.text}</StatusChip>
        <SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEMES} />
      </div>
    </header>
  );
}

/** Depth layer 0: the canvas colour with the owner's swirl image, loaded after first paint. Opacity and blur per
 *  theme come from --ds-canvas-* in styles.css. It sits behind the window and the rail, never under text. */
function Canvas() {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => { const img = new Image(); img.decoding = "async"; img.src = "bg/swirl.webp"; img.decode().then(() => live && setSrc(img.src)).catch(() => undefined); };
    if (document.readyState === "complete") { const t = window.setTimeout(load, 200); return () => { live = false; clearTimeout(t); }; }
    window.addEventListener("load", load, { once: true });
    return () => { live = false; window.removeEventListener("load", load); };
  }, []);
  return <div aria-hidden className="ds-canvas">{src && <img src={src} alt="" className="ds-canvas-img" />}</div>;
}

function Toasts() {
  const { toasts } = useApp();
  return (
    <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-[calc(76px+env(safe-area-inset-bottom))] z-50 flex flex-col items-end gap-2 md:bottom-4">
      {toasts.map((t) => (
        <div key={t.id} className="enter pointer-events-auto flex max-w-sm items-center gap-2 rounded-ds-lg bg-ds-surface px-4 py-3 text-ds-sm text-ds-text shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]">
          <span className={cx("size-2 shrink-0 rounded-full", t.tone === "ok" ? "bg-ds-success" : t.tone === "bad" ? "bg-ds-danger" : t.tone === "warn" ? "bg-ds-attention" : "bg-ds-muted")} />{t.text}
        </div>
      ))}
    </div>
  );
}

function ConfirmDialog() {
  const { confirmReq: c, closeConfirm } = useApp();
  const [val, setVal] = useState(""); const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { setVal(""); if (c) setTimeout(() => (ref.current?.querySelector("input,button[data-ok]") as HTMLElement | null)?.focus(), 30); }, [c]);
  if (!c) return null;
  const ok = () => closeConfirm(c.input ? val : true);
  return (
    <div className="fade-in fixed inset-0 z-50 flex items-center justify-center bg-[rgba(11,11,28,0.4)] p-4" onMouseDown={(e) => e.target === e.currentTarget && closeConfirm(null)} onKeyDown={(e) => { if (e.key === "Escape") closeConfirm(null); if (e.key === "Enter" && c.input) ok(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby="dlg-t" className="enter ds-root w-full max-w-md">
        <Card elevation={3}>
          <h2 id="dlg-t" className="text-ds-xl leading-ds-tight font-semibold">{c.title}</h2>
          {c.body && <p className="mt-2 text-ds-md leading-ds-normal text-ds-text-2">{c.body}</p>}
          {c.input && <Field label={c.input.label} htmlFor="dlg-in" className="mt-4"><Input id="dlg-in" value={val} placeholder={c.input.placeholder} onChange={(e) => setVal(e.target.value)} /></Field>}
          <div className="mt-6 flex justify-end gap-2">
            <Button variant="ghost" onClick={() => closeConfirm(null)}>Cancel</Button>
            <Button data-ok variant={c.danger ? "danger" : "primary"} onClick={ok}>{c.ok || "OK"}</Button>
          </div>
        </Card>
      </div>
    </div>
  );
}

function AskDrawer() {
  const { askOpen, setAskOpen, route } = useApp();
  useEffect(() => { if (route.name === "assistant") setAskOpen(false); }, [route.name, setAskOpen]);
  useEffect(() => { if (!askOpen) return; const on = (e: KeyboardEvent) => e.key === "Escape" && setAskOpen(false); addEventListener("keydown", on); return () => removeEventListener("keydown", on); }, [askOpen, setAskOpen]);
  if (!askOpen) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-[rgba(11,11,28,0.3)]" onMouseDown={(e) => e.target === e.currentTarget && setAskOpen(false)}>
      <aside aria-label="Ask Nightshift" className="enter ds-root flex w-full flex-col overflow-hidden bg-ds-app shadow-ds-3 max-sm:rounded-none sm:m-3 sm:max-w-[460px] sm:rounded-ds-xl">
        <div className="flex items-center justify-between border-b border-ds-border-subtle px-5 py-3">
          <div className="flex items-center gap-3"><Mark size={24} /><span className="text-ds-lg font-semibold">Ask Nightshift</span></div>
          <div className="flex items-center gap-1"><a href="#/assistant" className="ds-focus rounded-ds-sm px-3 py-1.5 text-ds-sm font-semibold text-ds-text-2 no-underline hover:bg-ds-hover hover:text-ds-text">Open full page</a><IconButton icon="x" label="Close" onClick={() => setAskOpen(false)} /></div>
        </div>
        <Chat compact />
      </aside>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="app-frame ds-root relative min-h-dvh">
      <Canvas />
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-ds-md focus:bg-ds-surface focus:px-4 focus:py-2">Skip to content</a>
      <SideTab />
      <div className="relative z-[1] p-2 md:py-4 md:pr-4 md:pl-3">
        <AppWindow glass className="mx-auto min-h-[calc(100dvh-16px)] max-w-[1600px] px-4 pt-4 pb-6 md:min-h-[calc(100dvh-32px)] md:px-6 lg:px-8 lg:pt-5">
          <TopBar />
          <main id="main" tabIndex={-1} className="mt-6 min-w-0 outline-none">{children}</main>
        </AppWindow>
      </div>
      <Toasts />
      <ConfirmDialog />
      <AskDrawer />
    </div>
  );
}
