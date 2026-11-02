/* API client. Talks to the FastAPI app under /api. When the very first /api/status call does not answer, it switches
   to the demo layer for the rest of the session so the UI is viewable without a backend. */
import type { ChatEvent, VoiceMade } from "./types";
import { demoChat, demoHandle } from "./demo";
import { ApiError } from "./errors";
export { ApiError };

const BASE = "/api";
const errMessage = async (res: Response) => {
  let msg = res.statusText || `HTTP ${res.status}`;
  try { const j = await res.json(); msg = j?.error?.message || j?.detail || msg; if (j?.error?.hint) msg += `. ${j.error.hint}`; } catch { /* not JSON */ }
  return msg;
};

const apiError = async (res: Response) => {
  let reason = res.statusText || `HTTP ${res.status}`; let code: string | undefined; let hint: string | undefined;
  try { const j = await res.json(); const e = j?.error; reason = e?.message || j?.detail || reason; code = e?.code; hint = e?.hint; } catch { /* not JSON */ }
  return new ApiError(hint ? `${reason}. ${hint}` : reason, code, reason, hint);
};

export const api = {
  demo: false,
  async request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    if (this.demo) return demoHandle(method, path, body);
    const init: RequestInit = { method, headers: {} };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) { (init.headers as Record<string, string>)["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
    const res = await fetch(BASE + path, init);
    if (!res.ok) throw await apiError(res);
    return (res.headers.get("content-type") || "").includes("json") ? res.json() : (res.text() as any);
  },
  get<T = any>(p: string) { return this.request<T>("GET", p); },
  post<T = any>(p: string, b?: unknown) { return this.request<T>("POST", p, b ?? {}); },
  put<T = any>(p: string, b: unknown) { return this.request<T>("PUT", p, b); },
  mediaUrl(id: string) { return `${BASE}/media/${id.split("/").map(encodeURIComponent).join("/")}`; },
  thumbUrl(id: string) { return `${this.mediaUrl(id)}/thumb.jpg`; },
  /** One of the three thumbnails the studio made for a long video (thumb_1.jpg to thumb_3.jpg). */
  thumbChoiceUrl(n: number, id: string) { return `${BASE}/thumbs/${n}/${id.split("/").map(encodeURIComponent).join("/")}`; },
  enc(id: string) { return id.split("/").map(encodeURIComponent).join("/"); },

  /** True once /status has answered. Only the very first call can switch to demo data (no backend at all, as in DEMO.cmd);
   *  after that a failed call throws, so a restarting service or one slow answer never swaps the real lists for sample ones. */
  answered: false,
  async status() {
    if (this.demo) return demoHandle("GET", "/status");
    try {
      const res = await fetch(BASE + "/status"); if (!res.ok) throw new Error(String(res.status));
      const j = await res.json(); this.answered = true; return j;
    } catch (e) {
      if (this.answered) throw e;
      this.demo = true;
      return demoHandle("GET", "/status");
    }
  },

  async voicePreview(voice: string, text: string) {
    if (this.demo) throw new Error("Voice samples need the engine running");
    const res = await fetch(BASE + "/voices/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ voice, text }) });
    if (!res.ok) throw new Error(await errMessage(res));
    return URL.createObjectURL(await res.blob());
  },

  /** POST /api/voices/design (CR-C3-UI-7): a voice from a description, previewed with `testLine`. Synchronous, can take about a
   *  minute the first time (the design model loads). The voice is stored under its name; `voice_id` is that name. */
  voiceDesign(name: string, description: string, testLine: string): Promise<VoiceMade> {
    return this.post("/voices/design", { name, description, test_line: testLine });
  },
  /** Uploads the recording (chunked, existing /api/uploads), then POST /api/voices/clone with the stored path. `consent` is always
   *  true here: the page does not call this until the owner has ticked the rights box, and the server refuses anything else. */
  async voiceClone(sample: File, name: string, testLine: string, onProgress: (pct: number) => void = () => {}): Promise<VoiceMade> {
    const up = await this.uploadFile(sample, onProgress);
    return this.post("/voices/clone", { name, sample_path: up.path, consent: true, test_line: testLine });
  },

  /** Chunked upload to /api/uploads (start, PUT 8 MB pieces at their offset, complete). Resolves with the stored path on the PC.
   *  The endpoint takes video containers and voice-sample audio (wav, mp3, m4a, flac, ogg, webm) under their own names. */
  async uploadFile(file: File, onProgress: (pct: number) => void): Promise<{ path: string; upload_id?: string }> {
    if (this.demo) { for (let p = 0; p <= 100; p += 25) { onProgress(p); await new Promise((r) => setTimeout(r, 120)); } return { path: `D:\Nightshift\imports\clipper\demo\sample-${file.size}.wav` }; }
    const start = await this.post<{ upload_id: string; chunk_size?: number }>("/uploads", { filename: file.name, size: file.size });
    const step = start.chunk_size || 8 * 1024 * 1024; const base = `/uploads/${encodeURIComponent(start.upload_id)}`;
    for (let off = 0; off < file.size; off += step) {
      const res = await fetch(`${BASE}${base}?offset=${off}`, { method: "PUT", body: file.slice(off, off + step) });
      if (!res.ok) throw await apiError(res);
      onProgress(Math.round((Math.min(file.size, off + step) / file.size) * 100));
    }
    const done = await this.post<{ path?: string | null; upload_id: string }>(`${base}/complete`);
    if (!done.path) throw new ApiError("The recording did not finish uploading");
    return { path: done.path, upload_id: done.upload_id };
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
