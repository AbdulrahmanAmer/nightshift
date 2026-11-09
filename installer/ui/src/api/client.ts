// Transport to the setup server (docs/design/installer-ui.md section 1.4). Same origin, cookie auth (nothing to do here).
// The real client talks JSON over fetch. The demo transport (src/demo) implements the same Api interface in memory; it is
// loaded on demand, only when ?demo or ?screen= is in the address or when the server answers with something that is not JSON.
import type { ActionBody, ActionName, ActionResult, AnswersResult, ChoicesDraft, DiskCheck, KeyCheck, ModelCheck, PickFolderResult, SetupState, Tier, UiStep, UninstallView } from "./types";

export interface Api {
  readonly demo: boolean;
  /** Long poll: resolves when the server's rev is above `since`, or after the server's own timeout. */
  getState(since: number, signal?: AbortSignal): Promise<SetupState>;
  uiStep(step: UiStep): Promise<ActionResult>;
  postAnswers(draft: ChoicesDraft): Promise<AnswersResult>;
  /** Ask the server to read an answers.json from a path and validate it like POST /api/answers. */
  loadAnswersFile(path: string): Promise<AnswersResult>;
  checkKey(service: "pexels" | "pixabay", key: string): Promise<KeyCheck>;
  disk(path: string, tier: Tier, source?: boolean): Promise<DiskCheck>;
  /** Opens the Windows folder dialog on the PC (the server shows it, in front of this window) and resolves when it closes. */
  pickFolder(target: "install" | "models", start: string, tier: Tier): Promise<PickFolderResult>;
  /** Does this Hugging Face GGUF exist, how big is it, does it fit a 16 GB card (the server does a HEAD request). */
  checkModel(repo: string, file: string): Promise<ModelCheck>;
  action(name: ActionName, body?: ActionBody): Promise<ActionResult>;
  /** null when this is not uninstall mode (HTTP 404). */
  getUninstall(): Promise<UninstallView | null>;
  uninstall(body: { deleteModels: boolean; deleteData: boolean }): Promise<ActionResult>;
  fileUrl(name: "voice-sample.wav" | "render-test.mp4" | "render-test.jpg" | "verify-report.json"): string;
  fetchText(name: "verify-report.json"): Promise<string>;
}

/** The answer was not JSON (for example the static page served for an API path). */
export class NotJsonError extends Error {
  constructor() { super("The server did not answer with JSON"); this.name = "NotJsonError"; }
}
export class HttpError extends Error {
  constructor(public status: number, message: string, public body: unknown) { super(message); this.name = "HttpError"; }
}

async function readJson<T>(res: Response): Promise<T> {
  const type = res.headers.get("content-type") ?? "";
  if (!type.includes("json")) throw res.status === 404 ? new HttpError(404, "Not found", null) : new NotJsonError();
  const body = (await res.json()) as T;
  if (!res.ok) {
    const m = (body as { message?: string } | null)?.message;
    throw new HttpError(res.status, m ?? `HTTP ${res.status}`, body);
  }
  return body;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  return readJson<T>(res);
}
const post = <T>(path: string, body?: unknown): Promise<T> =>
  call<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });

/** A 409 carries {ok:false, message}; the UI shows the message, so it is returned like any other ActionResult. */
async function postAction(path: string, body?: unknown): Promise<ActionResult> {
  try {
    return await post<ActionResult>(path, body);
  } catch (e) {
    if (e instanceof HttpError && e.status === 409) return { ok: false, message: e.message, state: undefined as unknown as SetupState };
    throw e;
  }
}

export const httpApi: Api = {
  demo: false,
  getState: (since, signal) => call<SetupState>(`/api/state?since=${since}`, { signal }),
  uiStep: (uiStep) => postAction("/api/ui-step", { uiStep }),
  postAnswers: (draft) => post<AnswersResult>("/api/answers", draft),
  loadAnswersFile: (path) => post<AnswersResult>("/api/answers", { path }),
  checkKey: (service, key) => post<KeyCheck>("/api/check-key", { service, key }),
  disk: (path, tier, source) => call<DiskCheck>(`/api/disk?path=${encodeURIComponent(path)}&tier=${tier}${source ? "&source=1" : ""}`),
  pickFolder: (target, start, tier) => post<PickFolderResult>("/api/pick-folder", { target, start, tier }),
  checkModel: (repo, file) => call<ModelCheck>(`/api/model-check?repo=${encodeURIComponent(repo)}&file=${encodeURIComponent(file)}`),
  action: (name, body) => postAction(`/api/action/${name}`, body ?? {}),
  async getUninstall() {
    try {
      return await call<UninstallView>("/api/uninstall");
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) return null;
      throw e;
    }
  },
  uninstall: (body) => postAction("/api/uninstall", body),
  fileUrl: (name) => `/api/file/${name}`,
  async fetchText(name) {
    const res = await fetch(`/api/file/${name}`, { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new HttpError(res.status, `HTTP ${res.status}`, null);
    return res.text();
  },
};

const params = new URLSearchParams(window.location.search);
export const wantsDemo = params.has("demo") || params.has("screen");
export const demoScreen = params.get("screen");

/** Loads the in-memory transport. Only called for `?demo` / `?screen`. The build has no code splitting (one `app.js`,
 *  see vite.installer.config.ts), so the fixtures are inside the bundle and this import resolves at once. */
export async function loadDemoApi(): Promise<Api> {
  const m = await import("../demo/fakeApi");
  return m.createDemoApi({ screen: demoScreen });
}
