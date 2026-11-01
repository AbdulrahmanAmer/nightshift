/* API client. Talks to the FastAPI app under /api. When /api/status does not answer, it switches
   to the demo layer for the rest of the session so the UI is viewable without a backend. */
import type { ChatEvent } from "./types";
import { demoChat, demoHandle } from "./demo";

const BASE = "/api";
const errMessage = async (res: Response) => {
  let msg = res.statusText || `HTTP ${res.status}`;
  try { const j = await res.json(); msg = j?.error?.message || j?.detail || msg; if (j?.error?.hint) msg += `. ${j.error.hint}`; } catch { /* not JSON */ }
  return msg;
};

export const api = {
  demo: false,
  async request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    if (this.demo) return demoHandle(method, path, body);
    const init: RequestInit = { method, headers: {} };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { (init.headers as Record<string, string>)["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const res = await fetch(BASE + path, init);
    if (!res.ok) throw new Error(await errMessage(res));
    return (res.headers.get("content-type") || "").includes("json") ? res.json() : (res.text() as any);
  },
  get<T = any>(p: string) { return this.request<T>("GET", p); },
  post<T = any>(p: string, b?: unknown) { return this.request<T>("POST", p, b ?? {}); },
  put<T = any>(p: string, b: unknown) { return this.request<T>("PUT", p, b); },
  mediaUrl(id: string) { return `${BASE}/media/${id.split("/").map(encodeURIComponent).join("/")}`; },
  thumbUrl(id: string) { return `${this.mediaUrl(id)}/thumb.jpg`; },
  enc(id: string) { return id.split("/").map(encodeURIComponent).join("/"); },

  /** First status call decides real vs demo. */
  async status() {
    if (!this.demo) {
      try { const res = await fetch(BASE + "/status"); if (!res.ok) throw new Error(String(res.status)); return await res.json(); }
      catch { this.demo = true; }
    }
    return demoHandle("GET", "/status");
  },

  async voicePreview(voice: string, text: string) {
    if (this.demo) throw new Error("Voice samples need the engine running");
    const res = await fetch(BASE + "/voices/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ voice, text }) });
    if (!res.ok) throw new Error(await errMessage(res));
    return URL.createObjectURL(await res.blob());
  },

  upload<T = any>(path: string, fd: FormData, onProgress: (pct: number) => void): Promise<T> {
    if (this.demo) return (async () => { for (let p = 0; p <= 100; p += 20) { onProgress(p); await new Promise((r) => setTimeout(r, 150)); } return demoHandle("POST", path, fd); })();
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); xhr.open("POST", BASE + path);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) { try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({} as T); } return; }
        let m = xhr.statusText; try { const j = JSON.parse(xhr.responseText); m = j?.error?.message || j?.detail || m; } catch { /* keep status text */ }
        reject(new Error(m || `HTTP ${xhr.status}`));
      };
      xhr.onerror = () => reject(new Error("Upload failed. Is the engine running?"));
      xhr.send(fd);
    });
  },

  /** Streams /api/assistant/chat (server-sent events over a POST response). */
  async *chat(message: string, conversationId: string | null, context: Record<string, unknown>): AsyncGenerator<ChatEvent> {
    if (this.demo) { yield* demoChat(message); return; }
    const res = await fetch(BASE + "/assistant/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, conversation_id: conversationId, context }) });
    if (!res.ok || !res.body) throw new Error(await errMessage(res));
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i: number;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (data) { try { yield JSON.parse(data) as ChatEvent; } catch { /* ignore a malformed frame */ } }
      }
    }
  },
};
