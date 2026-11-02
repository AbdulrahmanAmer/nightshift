/* App shell: the canvas (public/bg/canvas.svg under a theme wash) -> SideTab rail -> the page, straight on the canvas,
   under a top bar pill. Cards keep their opaque surfaces. Toasts, the confirm dialog and the Ask drawer sit above it.
   In the WebView2 app host a 36 px TitleBar comes first (renders nothing in a browser). */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import { fmtClock, fmtDay, fmtWhen, stageWords, store } from "../lib/format";
import type { Job, Status } from "../lib/types";
import { cx } from "../ui/kit";
import { Chat } from "../pages/Assistant";
import { jobTitle } from "../pages/Queue";
import { Mark } from "../ui/Mark";
import { Button, Card, CardMenu, DigitalClock, Field, Icon, IconButton, Input, Kbd, SegmentedControl, StatusChip, StatusDot, type SegmentOption } from "../components/ref12";
import { startAutoTitle } from "../lib/autoTitle";
import { SideTab } from "./SideTab";
import { TitleBar } from "./TitleBar";
import { host } from "../lib/host";
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

/** Close a popover on Escape and on a press outside `ref`. */
function useDismiss(open: boolean, close: () => void, ref: React.RefObject<HTMLElement>) {
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    addEventListener("pointerdown", away); addEventListener("keydown", esc);
    return () => { removeEventListener("pointerdown", away); removeEventListener("keydown", esc); };
  }, [open, close, ref]);
}

/** The search field is a button that opens an inline Assistant panel under it (the compact Chat). Typing a character
 *  on it opens the panel with that character in the message box; Enter in the panel sends; Esc closes. On the
 *  Assistant page it moves focus to the page's own message box instead. */
function AskField({ className }: { className?: string }) {
  const { route } = useApp();
  const [open, setOpen] = useState(false); const [seed, setSeed] = useState("");
  const ref = useRef<HTMLDivElement>(null); const btn = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); btn.current?.focus(); };
  useDismiss(open, () => setOpen(false), ref);
  const start = (initial = "") => {
    if (route.name === "assistant") { const ta = document.getElementById("chat-text") as HTMLTextAreaElement | null; ta?.focus(); return; }
    setSeed(initial); setOpen(true);
  };
  return (
    <div ref={ref} className={cx("relative", className)} onKeyDown={(e) => { if (e.key === "Escape" && open) { e.stopPropagation(); close(); } }}>
      <button ref={btn} type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => (open ? setOpen(false) : start())}
        onKeyDown={(e) => { if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); start(e.key); } }}
        className="ds-focus flex h-9 w-full min-w-0 items-center gap-2 rounded-ds-md border border-ds-border bg-ds-surface px-3 text-left text-ds-xs text-ds-muted shadow-ds-inset-sm transition-[border-color] duration-[120ms] hover:border-ds-focus">
        <Icon name="search" size={15} /><span className="min-w-0 flex-1 truncate">Ask Nightshift or search</span><Kbd>Enter</Kbd>
      </button>
      {open && (
        <div role="dialog" aria-label="Ask Nightshift" className="fade-in absolute top-full left-0 z-40 mt-2 flex h-[min(560px,70dvh)] w-[min(460px,calc(100vw-32px))] flex-col overflow-hidden rounded-ds-xl bg-ds-app shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]">
          <div className="flex items-center justify-between border-b border-ds-border-subtle px-4 py-2.5">
            <span className="flex items-center gap-2 text-ds-sm font-semibold"><Mark size={20} />Ask Nightshift</span>
            <div className="flex items-center gap-1"><a href="#/assistant" onClick={() => setOpen(false)} className="ds-focus rounded-ds-sm px-2 py-1 text-ds-xs font-semibold text-ds-text-2 no-underline hover:bg-ds-hover">Open full page</a><IconButton icon="x" label="Close" size="sm" onClick={close} /></div>
          </div>
          <Chat compact initialText={seed} key={seed} />
        </div>
      )}
    </div>
  );
}

/** Bell: a popover of recent job events (the same /jobs data as the Queue's Job events card), mark all read,
 *  and a link to the Queue. It never navigates by itself. Read state is kept in this browser. */
function Notifications() {
  const { status, go } = useApp();
  const [open, setOpen] = useState(false); const ref = useRef<HTMLDivElement>(null);
  const [readAt, setReadAt] = useState(() => store.get<string>("ns-notif-read", ""));
  // Waits for the first status so the demo switch has happened (no stray request to a missing backend).
  const jobs = useLoad<Job[]>(() => (status ? api.get("/jobs") : Promise.resolve([])), [status?.engine?.state], []);
  const { channelName } = useApp();
  useDismiss(open, () => setOpen(false), ref);
  const events = jobs.data.filter((j) => !["queued", "running"].includes(j.status)).slice(0, 6);
  const unread = events.filter((j) => (j.created_at || "") > readAt);
  const markRead = () => { const t = new Date().toISOString(); setReadAt(t); store.set("ns-notif-read", t); };
  const label = unread.length ? `Notifications, ${unread.length} unread` : "Notifications";
  return (
    <div ref={ref} className="relative">
      <IconButton icon="bell" label={label} dot={unread.length > 0 || !!status?.last_error} aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)} />
      {open && (
        <div role="dialog" aria-label="Notifications" className="fade-in absolute top-full right-0 z-40 mt-2 w-[min(380px,calc(100vw-32px))] rounded-ds-xl bg-ds-surface p-2 shadow-[var(--ds-shadow-3),0_0_0_1px_var(--ds-border-subtle)]">
          <div className="flex items-center justify-between px-2 py-1.5">
            <p className="text-ds-md font-semibold">Notifications</p>
            <Button variant="ghost" size="sm" onClick={markRead} disabled={!unread.length}>Mark all read</Button>
          </div>
          {events.length ? (
            <ul className="flex max-h-[min(420px,60dvh)] flex-col gap-1 overflow-y-auto scroll-thin">
              {events.map((j) => {
                const failed = j.status === "failed"; const isNew = (j.created_at || "") > readAt;
                return (
                  <li key={j.job_id} className={cx("flex gap-3 rounded-ds-md px-2 py-2", isNew && "bg-ds-accent-subtle")}>
                    <StatusDot tone={failed ? "danger" : j.status === "cancelled" ? "attention" : "online"} className="mt-1.5" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-ds-sm font-semibold">{jobTitle(j, channelName)}</p>
                      <p className="line-clamp-2 text-ds-2xs text-ds-text-2">{failed ? `Stopped at ${stageWords(j.stage).toLowerCase()}. ${j.message || ""}` : j.status === "cancelled" ? "Cancelled" : `Done · ${j.result?.output_ids?.length ? `${j.result.output_ids.length} videos` : "1 video"}`}</p>
                      <p className="text-ds-2xs text-ds-muted ds-num">{fmtDay(j.created_at)} {fmtClock(j.created_at)}</p>
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : <p className="px-2 py-4 text-ds-sm text-ds-text-2">Nothing yet. Finished and stopped jobs show up here</p>}
          <div className="mt-1 border-t border-ds-border-subtle px-2 pt-2"><Button variant="ghost" size="sm" trailingIcon="chevronRight" className="-ml-2" onClick={() => { setOpen(false); markRead(); go("#/"); }}>Open Queue</Button></div>
        </div>
      )}
    </div>
  );
}

function TopBar() {
  const { status, demo, ask, route, theme, setTheme, confirm, toast, refresh } = useApp();
  const eng = engineText(status);
  const [title, sub] = TITLES[route.name] || TITLES.queue!;
  const quit = async () => {
    const ok = await confirm({ title: "Quit Nightshift?", body: "Stops the engine and frees the graphics card.", ok: "Quit", danger: true });
    if (ok !== true) return;
    try { await api.post("/engine/shutdown", { grace_s: 30 }); toast("Shutting down. You can close this tab", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); }
  };
  return (
    <header className="ds-bar @container rounded-ds-xl px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-3 @min-[1100px]:flex-nowrap">
        <div className="mr-auto min-w-0 @min-[1100px]:mr-0 @min-[1100px]:w-[188px] @min-[1100px]:shrink-0">
          <h1 className="truncate text-ds-xl leading-ds-tight font-semibold tracking-[-0.01em]">{title}</h1>
          <p className="truncate text-ds-2xs text-ds-text-2">{demo ? "Sample data: no engine answered" : sub}</p>
        </div>
        <AskField className="order-last basis-full @min-[760px]:order-none @min-[760px]:basis-auto @min-[760px]:flex-[1_1_220px] @min-[1100px]:max-w-[340px]" />
        <div className="hidden min-w-0 items-center gap-2 @min-[760px]:flex @min-[1100px]:ml-auto">
          <StatusChip tone={DOT[eng.tone]} live title="What the engine is doing">{eng.text}</StatusChip>
          <span className="hidden @min-[960px]:contents"><StatusChip tone="muted" title="Next automatic run">{status?.next_run ? <>Next run <span className="ds-num">{fmtWhen(status.next_run)}</span></> : "Automatic runs off"}</StatusChip></span>
        </div>
        <span className="hidden @min-[1180px]:contents"><DigitalClock variant="inline" /></span>
        <span className="hidden @min-[760px]:contents"><SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEMES} /></span>
        <div className="flex items-center gap-1">
          <Notifications />
          <CardMenu icon="moreVertical" size="md" label="More" items={[{ label: "Quit Nightshift", icon: "power", danger: true, onSelect: quit }]} />
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

/** Depth layer 0: the canvas colour, the generated marbled canvas (public/bg/canvas.svg, loaded after first paint)
 *  at --ds-canvas-opacity, and the theme wash over it (strongest top left, where the greeting and the top bar sit). */
function Canvas() {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const load = () => { const img = new Image(); img.decoding = "async"; img.src = "bg/canvas.svg"; img.decode().then(() => live && setSrc(img.src)).catch(() => undefined); };
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
          <div className="mt-6 flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={() => closeConfirm(null)}>Cancel</Button>
            {c.alt && <Button variant="soft" onClick={() => closeConfirm("alt")}>{c.alt}</Button>}
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

function OfflineBanner() {
  const { offline, demo } = useApp();
  if (!offline || demo) return null;
  return <p role="status" className="mt-4 rounded-ds-md bg-ds-danger-soft px-4 py-3 text-ds-sm"><strong className="font-semibold">Engine not answering.</strong> Nightshift keeps trying and will carry on by itself. What you see below is the last thing it told us.</p>;
}

export function Shell({ children }: { children: ReactNode }) {
  useEffect(() => startAutoTitle(), []);
  return (
    <div className="app-frame ds-root relative min-h-dvh">
      <Canvas />
      <TitleBar />
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-ds-md focus:bg-ds-surface focus:px-4 focus:py-2">Skip to content</a>
      <SideTab brand={!host.available()} />
      <div className={cx("relative z-[1] mx-auto max-w-[1600px] px-3 pb-6 md:px-6", host.available() ? "pt-11" : "pt-3 md:pt-4")}>
        <TopBar />
        <OfflineBanner />
        <main id="main" tabIndex={-1} className="mt-6 min-w-0 outline-none">{children}</main>
      </div>
      <Toasts />
      <ConfirmDialog />
      <AskDrawer />
    </div>
  );
}
