import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import { store } from "./format";
import type { Status } from "./types";

export type Theme = "light" | "dark" | "soft";
export type Toast = { id: number; text: string; tone?: "ok" | "bad" | "warn" };
export type ConfirmReq = { title: string; body?: string; ok?: string; danger?: boolean; input?: { label: string; placeholder?: string }; /** A second choice next to OK; picking it resolves to "alt". */ alt?: string; resolve: (v: string | boolean | null) => void };

interface AppCtx {
  status: Status | null; demo: boolean; /** The engine stopped answering after it had answered before: show it and keep retrying. */ offline: boolean; refresh: () => Promise<void>;
  toast: (text: string, tone?: Toast["tone"]) => void; toasts: Toast[];
  confirm: (r: Omit<ConfirmReq, "resolve">) => Promise<string | boolean | null>; confirmReq: ConfirmReq | null; closeConfirm: (v: string | boolean | null) => void;
  route: { name: string; query: URLSearchParams }; go: (hash: string) => void;
  askOpen: boolean; setAskOpen: (v: boolean) => void; askContext: Record<string, unknown>; ask: (ctx?: Record<string, unknown>, prompt?: string, openDrawer?: boolean) => void; askPrompt: string | null; clearAskPrompt: () => void;
  theme: Theme; setTheme: (t: Theme) => void;
  channelName: (slug?: string | null) => string;
  /** The name chosen in the setup window (config/ui_prefs.json), or null. */
  ownerName: string | null;
}
const Ctx = createContext<AppCtx>(null as unknown as AppCtx);
export const useApp = () => useContext(Ctx);

const ROUTES: Record<string, string> = { "": "queue", queue: "queue", create: "create", library: "library", assistant: "assistant", channels: "channels", results: "results", settings: "settings", design: "design", "design/ref12": "design-ref12" };
const parseHash = () => { const [n, q] = location.hash.replace(/^#\/?/, "").split("?"); return { name: ROUTES[n] ?? "queue", query: new URLSearchParams(q || "") }; };

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [demo, setDemo] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmReq, setConfirmReq] = useState<ConfirmReq | null>(null);
  const [route, setRoute] = useState(parseHash);
  const [askOpen, setAskOpen] = useState(false);
  const [askContext, setAskContext] = useState<Record<string, unknown>>({});
  const [askPrompt, setAskPrompt] = useState<string | null>(null);
  const [ownerName, setOwnerName] = useState<string | null>(null);
  const [theme, setThemeState] = useState<Theme>(() => store.get<Theme | null>("ns-theme", null) ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  const tid = useRef(0);

  const lastStatus = useRef("");
  const [offline, setOffline] = useState(false);
  // Only publish a new status when something changed, so neither the demo poll nor the 2 s heartbeat re-renders every page.
  const publish = useCallback((s: Status) => { const key = JSON.stringify(s); if (key !== lastStatus.current) { lastStatus.current = key; setStatus(s); } }, []);
  /** One status call; true when the engine answered. A failure keeps the last status (never swaps in sample data). */
  const poll = useCallback(async () => {
    try { publish(await api.status()); setDemo(api.demo); setOffline(false); return true; }
    catch { setOffline(true); return false; }
  }, [publish]);
  const refresh = useCallback(async () => { await poll(); }, [poll]);
  const toast = useCallback((text: string, tone?: Toast["tone"]) => {
    const id = ++tid.current; setToasts((t) => [...t, { id, text, tone }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  // Supervisor events the dashboard stream keeps as their own kind (F-D2): the weekly update check found a newer version, the nightly run finished.
  const notifyUpdate = useCallback((p: { update_available?: boolean; latest?: string; current?: string } | null) => {
    if (p?.update_available && p.latest) toast(`Version ${p.latest} is available${p.current ? ` (you have ${p.current})` : ""}. Open Settings, Updates`, "ok");
  }, [toast]);
  const notifyRun = useCallback((p: { event?: string } | null) => {
    if (p?.event === "finished") toast("The nightly run has finished. New videos wait in the Queue", "ok");
  }, [toast]);
  const confirm = useCallback((r: Omit<ConfirmReq, "resolve">) => new Promise<string | boolean | null>((resolve) => setConfirmReq({ ...r, resolve })), []);
  const closeConfirm = useCallback((v: string | boolean | null) => { setConfirmReq((c) => { c?.resolve(v); return null; }); }, []);
  const go = useCallback((hash: string) => { location.hash = hash; }, []);
  // openDrawer=false hands the prompt to the Assistant page instead (the top bar search routes there).
  const ask = useCallback((ctx?: Record<string, unknown>, prompt?: string, openDrawer = true) => { setAskContext(ctx || {}); if (prompt) setAskPrompt(prompt); if (openDrawer) setAskOpen(true); }, []);
  const setTheme = useCallback((t: Theme) => { setThemeState(t); store.set("ns-theme", t); }, []);
  const channelName = useCallback((slug?: string | null) => status?.channels?.find((c) => c.slug === slug)?.display_name || slug || "", [status]);

  // First start: the setup window's choices (config/ui_prefs.json, served with /settings). The name is shown in the greeting; the
  // theme is applied only while this browser has no saved choice, and is saved then, so a later pick in Settings always wins.
  useEffect(() => {
    api.get("/settings").then((s: { owner_name?: string | null; ui_theme?: Theme | null }) => {
      if (s?.owner_name) setOwnerName(s.owner_name);
      if (s?.ui_theme && store.get<Theme | null>("ns-theme", null) === null) setTheme(s.ui_theme);
    }).catch(() => { /* demo data or no settings yet */ });
  }, [setTheme]);
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => { const on = () => setRoute(parseHash()); addEventListener("hashchange", on); return () => removeEventListener("hashchange", on); }, []);
  useEffect(() => {
    let es: EventSource | null = null; let timer = 0; let stopped = false; let fails = 0;
    // Poll every 10 s (1.5 s for the demo); after a failure retry with backoff (2, 4, 8 ... up to 30 s) instead of giving up.
    const tick = async () => {
      const visible = document.visibilityState === "visible";
      if (visible || fails > 0) fails = (await poll()) ? 0 : fails + 1;
      if (!stopped) timer = window.setTimeout(tick, api.demo ? 1500 : fails ? Math.min(30000, 1000 * 2 ** Math.min(fails, 5)) : 10000);
    };
    poll().then((ok) => {
      if (stopped) return;
      fails = ok ? 0 : 1;
      if (!api.demo && "EventSource" in window) {
        try { es = new EventSource("/api/events"); es.onmessage = (ev) => { try { const m = JSON.parse(ev.data); if (m.kind === "status") { publish(m.payload); setOffline(false); fails = 0; } else if (m.kind === "update") notifyUpdate(m.payload); else if (m.kind === "run") notifyRun(m.payload); } catch { /* ignore */ } }; } catch { /* polling covers it */ }
      }
      timer = window.setTimeout(tick, api.demo ? 1500 : fails ? 2000 : 10000);
    });
    return () => { stopped = true; es?.close(); clearTimeout(timer); };
  }, [poll, publish, notifyUpdate, notifyRun]);

  const value = useMemo<AppCtx>(() => ({ status, demo, offline, refresh, toast, toasts, confirm, confirmReq, closeConfirm, route, go, askOpen, setAskOpen, askContext, ask, askPrompt, clearAskPrompt: () => setAskPrompt(null), theme, setTheme, channelName, ownerName }),
    [status, demo, offline, refresh, toast, toasts, confirm, confirmReq, closeConfirm, route, go, askOpen, askContext, ask, askPrompt, theme, setTheme, channelName, ownerName]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Fetch helper for pages: loads on mount and whenever deps change; reload() re-runs it. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[], fallback: T) {
  const [data, setData] = useState<T>(fallback);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true; setLoading(true);
    fn().then((d) => { if (live) { setData(d); setError(null); } }).catch((e) => { if (live) setError(e.message); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, setData, loading, error, reload: () => setN((x) => x + 1) };
}
