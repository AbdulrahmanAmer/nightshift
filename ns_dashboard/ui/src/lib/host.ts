/* App host bridge (docs/design/installer-ui.md §6.3): the frameless WebView2 window posts window-state and
   close-requested events; the page sends window commands. Everything is a no-op in a normal browser. */
export type HostCommand =
  | { cmd: "minimize" }
  | { cmd: "toggle-maximize" }
  | { cmd: "drag-start" }
  | { cmd: "close" }
  | { cmd: "close-ack" }
  | { cmd: "ready" };

export type HostEvent =
  | { event: "window-state"; state: "normal" | "maximized" | "minimized" }
  | { event: "close-requested" };

interface WebView2Bridge {
  postMessage(message: HostCommand): void;
  addEventListener(type: "message", listener: (e: MessageEvent<HostEvent>) => void): void;
  removeEventListener(type: "message", listener: (e: MessageEvent<HostEvent>) => void): void;
}
declare global {
  interface Window { chrome?: { webview?: WebView2Bridge } }
}

export const host = {
  available: (): boolean => !!window.chrome?.webview,
  send: (c: HostCommand): void => window.chrome?.webview?.postMessage(c),
  on: (fn: (e: HostEvent) => void): (() => void) => {
    const w = window.chrome?.webview;
    if (!w) return () => {};
    const l = (e: MessageEvent<HostEvent>) => fn(e.data);
    w.addEventListener("message", l);
    return () => w.removeEventListener("message", l);
  },
};
