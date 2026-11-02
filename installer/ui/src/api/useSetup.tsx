// The one hook that owns the connection: the long poll on /api/state, the uninstall view, the action helpers, the toasts
// and the text the single aria-live region announces. Screens read it through useSetup().
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { HttpError, NotJsonError, demoScreen, httpApi, loadDemoApi, wantsDemo, type Api } from "./client";
import type { ActionBody, ActionName, ActionResult, SetupState, UiStep, UninstallView } from "./types";

export type Conn = "connecting" | "ok" | "lost";
export interface ToastItem { id: number; text: string }

export interface SetupApi {
  api: Api | null;
  demo: boolean;
  /** SetupState; null until the first answer, and always null in uninstall mode. */
  state: SetupState | null;
  /** undefined while unknown, null when this is not uninstall mode. */
  uninstall: UninstallView | null | undefined;
  conn: Conn;
  busy: ReadonlySet<string>;
  toasts: ToastItem[];
  announcement: string;
  act: (name: ActionName, body?: ActionBody) => Promise<ActionResult>;
  setUiStep: (step: UiStep) => Promise<void>;
  removeNow: (body: { deleteModels: boolean; deleteData: boolean }) => Promise<void>;
  applyState: (s: SetupState) => void;
  toast: (text: string) => void;
  announce: (text: string) => void;
}

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));
const isNoServer = (e: unknown) => e instanceof NotJsonError || (e instanceof HttpError && e.status === 404);

export function useSetupProvider(): SetupApi {
  const [api, setApi] = useState<Api | null>(null);
  const [state, setState] = useState<SetupState | null>(null);
  const [uninstall, setUninstall] = useState<UninstallView | null | undefined>(undefined);
  const [conn, setConn] = useState<Conn>("connecting");
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [announcement, setAnnouncement] = useState("");
  const revRef = useRef(0);
  const apiRef = useRef<Api | null>(null);
  const toastId = useRef(0);

  const applyState = useCallback((s: SetupState) => {
    if (s.rev >= revRef.current) { revRef.current = s.rev; setState(s); }
  }, []);
  const announce = useCallback((text: string) => setAnnouncement(text), []);
  const toast = useCallback((text: string) => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-2), { id, text }]);
    setAnnouncement(text);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);

  // Boot, then follow the server.
  useEffect(() => {
    let alive = true;
    const ac = new AbortController();
    (async () => {
      let a: Api = wantsDemo ? await loadDemoApi() : httpApi;
      let failSince: number | null = null;
      const fail = () => {
        failSince ??= Date.now();
        if (Date.now() - failSince >= 6000) setConn("lost");
      };
      // 1. Uninstall mode or not (GET /api/uninstall: 200 yes, 404 no).
      let view: UninstallView | null | undefined;
      while (alive && view === undefined) {
        try {
          view = await a.getUninstall();
        } catch (e) {
          if (!a.demo && isNoServer(e)) { a = await loadDemoApi(); continue; }
          fail();
          await sleep(2000);
        }
      }
      if (!alive) return;
      apiRef.current = a;
      setApi(a);
      setUninstall(view ?? null);
      if (view) {
        // Uninstall mode: only the uninstall view is polled.
        setConn("ok");
        let current: UninstallView = view;
        while (alive) {
          await sleep(current.running ? 1200 : 3000);
          try { current = (await a.getUninstall()) ?? current; if (alive) { setUninstall(current); setConn("ok"); failSince = null; } } catch { fail(); }
        }
        return;
      }
      // 2. The state long poll.
      while (alive) {
        try {
          const s = await a.getState(revRef.current, ac.signal);
          if (!alive) break;
          revRef.current = s.rev;
          setState(s);
          setConn("ok");
          failSince = null;
        } catch (e) {
          if (!alive) break;
          if (!a.demo && isNoServer(e)) { a = await loadDemoApi(); apiRef.current = a; setApi(a); revRef.current = 0; continue; }
          fail();
          await sleep(2000);
        }
      }
    })();
    return () => { alive = false; ac.abort(); };
  }, []);

  const mark = useCallback(async <T,>(key: string, fn: () => Promise<T>): Promise<T> => {
    setBusy((b) => new Set(b).add(key));
    try { return await fn(); } finally { setBusy((b) => { const n = new Set(b); n.delete(key); return n; }); }
  }, []);

  const act = useCallback(async (name: ActionName, body?: ActionBody): Promise<ActionResult> => {
    const a = apiRef.current;
    if (!a) return { ok: false, message: null, state: undefined as unknown as SetupState };
    return mark(name, async () => {
      try {
        const r = await a.action(name, body);
        if (r.state) applyState(r.state);
        if (!r.ok && r.message) toast(r.message);
        return r;
      } catch (e) {
        const message = e instanceof Error ? e.message : "Setup did not answer";
        toast(message);
        return { ok: false, message, state: undefined as unknown as SetupState };
      }
    });
  }, [applyState, mark, toast]);

  const setUiStep = useCallback(async (step: UiStep) => {
    const a = apiRef.current;
    if (!a) return;
    await mark(`ui-step:${step}`, async () => {
      try {
        const r = await a.uiStep(step);
        if (r.state) applyState(r.state);
        if (!r.ok && r.message) toast(r.message);
      } catch (e) { toast(e instanceof Error ? e.message : "Setup did not answer"); }
    });
  }, [applyState, mark, toast]);

  const removeNow = useCallback(async (body: { deleteModels: boolean; deleteData: boolean }) => {
    const a = apiRef.current;
    if (!a) return;
    await mark("uninstall", async () => {
      try {
        const r = await a.uninstall(body);
        if (!r.ok && r.message) toast(r.message);
        const v = await a.getUninstall();
        if (v) setUninstall(v);
      } catch (e) { toast(e instanceof Error ? e.message : "Setup did not answer"); }
    });
  }, [mark, toast]);

  return useMemo(() => ({
    api, demo: !!api?.demo, state, uninstall, conn, busy, toasts, announcement, act, setUiStep, removeNow, applyState, toast, announce,
  }), [api, state, uninstall, conn, busy, toasts, announcement, act, setUiStep, removeNow, applyState, toast, announce]);
}

export const SetupContext = createContext<SetupApi | null>(null);
export function useSetup(): SetupApi {
  const v = useContext(SetupContext);
  if (!v) throw new Error("useSetup outside SetupContext");
  return v;
}
/** Screens render only once the state exists, so they can read it without a null check. */
export function useLiveState(): SetupApi & { state: SetupState; api: Api } {
  const s = useSetup();
  if (!s.state || !s.api) throw new Error("useLiveState before the first state");
  return s as SetupApi & { state: SetupState; api: Api };
}
export { demoScreen };
