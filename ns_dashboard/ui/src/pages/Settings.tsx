import { useEffect, useState, type ReactNode } from "react";
import { Copy, Download, FileArchive, Github, KeyRound, Lock, LockOpen, RefreshCw, Trash2 } from "lucide-react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import type { LogLine, Settings as S } from "../lib/types";
import { Button, Card, cx, PageHeader, SegTabs, Toggle } from "../ui/kit";

function Row({ title, sub, children, className }: { title: string; sub?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cx("flex flex-col gap-3 border-b border-line py-4 last:border-0 sm:flex-row sm:items-center sm:justify-between sm:gap-6", className)}>
      <div className="min-w-0"><p className="text-[14px] font-medium">{title}</p>{sub && <p className="mt-0.5 text-[13px] text-text-2">{sub}</p>}</div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

export default function Settings() {
  const { toast, confirm, demo, status } = useApp();
  const st = useLoad<S | null>(() => api.get("/settings"), [], null);
  const s = st.data;
  const [time, setTime] = useState("03:00");
  const [unlocked, setUnlocked] = useState(false);
  const [flags, setFlags] = useState<Record<string, string>>({});
  const [level, setLevel] = useState("");
  const [upd, setUpd] = useState<{ current?: string; latest?: string; up_to_date?: boolean; notes?: string } | null>(null);
  const [checking, setChecking] = useState(false);
  const logs = useLoad<LogLine[]>(() => api.get(`/logs/tail?n=200&level=${encodeURIComponent(level)}`), [level], []);
  useEffect(() => { if (s) { setTime(s.schedule_time || "03:00"); setFlags(Object.fromEntries(Object.entries(s.flags || {}).map(([k, v]) => [k, String(v)]))); } }, [s]);
  useEffect(() => { const t = setInterval(() => document.visibilityState === "visible" && logs.reload(), 5000); return () => clearInterval(t); }, [level]); // eslint-disable-line react-hooks/exhaustive-deps

  const put = async (body: Record<string, unknown>, ok: string) => { try { const r = await api.put("/settings", body); if (r && typeof r === "object") st.setData({ ...s, ...r }); toast(ok, "ok"); return true; } catch (e: any) { toast(e.message, "bad"); return false; } };
  const setKey = async (name: string) => {
    const v = await confirm({ title: `${name[0].toUpperCase() + name.slice(1)} key`, body: "Paste the free key from your account page. It is stored in Windows Credential Manager, never in a file", ok: "Save key", input: { label: "Key", placeholder: "Paste the key" } });
    if (typeof v !== "string" || !v.trim()) return;
    try { await api.post("/settings/keys", { name, value: v.trim() }); toast("Key saved", "ok"); st.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const check = async () => { setChecking(true); try { setUpd(await api.get("/update/check")); } catch (e: any) { toast(e.message, "bad"); } setChecking(false); };
  const apply = async () => {
    const ok = await confirm({ title: "Install the update?", body: "Nightshift stops the engine, installs the new version and starts again. If anything fails it goes back to this version", ok: "Install" });
    if (!ok) return; try { await api.post("/update/apply"); toast("Updating. The page reconnects when it is done", "ok"); } catch (e: any) { toast(e.message, "bad"); }
  };
  const diag = async () => { try { const r = await api.post("/diagnostics"); toast(`Report saved to ${r?.path || "your Desktop"}`, "ok"); } catch (e: any) { toast(e.message, "bad"); } };
  const copy = async (t: string) => { try { await navigator.clipboard.writeText(t); toast("Path copied", "ok"); } catch { toast("Copy was blocked", "warn"); } };
  const home = s?.home || status?.disk?.home || "";
  const version = s?.version || status?.version || upd?.current;

  return (
    <div className="enter flex flex-col gap-5">
      <PageHeader title="Settings" sub="The defaults from setup work. Change things here only if you want to" />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="flex flex-col gap-5">
          <Card className="px-6 py-2">
            <h2 className="pt-4 text-[17px] font-medium">Overnight work</h2>
            <Row title="Automatic run time" sub="Nightshift wakes the PC at this time and works while you sleep">
              <input aria-label="Run time" type="time" className="field-input w-[140px] font-mono" value={time} onChange={(e) => setTime(e.target.value)} />
              <Button variant="primary" size="sm" className="h-10" disabled={time === s?.schedule_time} onClick={() => put({ schedule_time: time }, `Runs at ${time} from tonight`)}>Save</Button>
            </Row>
            <Row title="Notifications" sub="A Windows notification when a run finishes or stops">
              <Toggle on={!!s?.notifications} onChange={(v) => { st.setData({ ...s, notifications: v }); put({ notifications: v }, v ? "Notifications on" : "Notifications off"); }} label="Notifications" />
            </Row>
          </Card>
          <Card className="px-6 py-2">
            <h2 className="pt-4 text-[17px] font-medium">Stock footage keys</h2>
            {["pexels", "pixabay"].map((k) => (
              <Row key={k} title={k[0].toUpperCase() + k.slice(1)} sub={s?.keys?.[k] ? <span className="font-mono">••••••••••••••••</span> : "Missing. Stories may run short of footage"}>
                <Button size="sm" variant={s?.keys?.[k] ? "secondary" : "primary"} icon={<KeyRound size={14} strokeWidth={1.5} />} onClick={() => setKey(k)}>{s?.keys?.[k] ? "Replace" : "Add key"}</Button>
              </Row>
            ))}
          </Card>
          <Card className="px-6 py-2">
            <h2 className="pt-4 text-[17px] font-medium">Where things live</h2>
            {[["Everything", home], ["Videos waiting for you", home && `${home}\\outbox`], ["Models", home && `${home}\\models`], ["Logs", home && `${home}\\logs`]].map(([l, p]) => (
              <Row key={l} title={l} sub={<span className="font-mono text-[12px] break-all">{p || "Unknown until the engine answers"}</span>}>{p && <Button size="sm" variant="ghost" icon={<Copy size={14} strokeWidth={1.5} />} onClick={() => copy(p)}>Copy</Button>}</Row>
            ))}
            {status?.disk && <Row title="Free space" sub={`On the drive that holds ${home}`}><span className="font-mono text-[14px] num">{status.disk.free_gb} GB of {status.disk.total_gb} GB</span></Row>}
          </Card>
        </div>

        <div className="flex flex-col gap-5">
          <Card className="px-6 py-2">
            <h2 className="pt-4 text-[17px] font-medium">Update</h2>
            <Row title="From GitHub" sub={upd ? (upd.up_to_date ? `You have the newest version, ${upd.current}` : `Version ${upd.latest} is ready. You have ${upd.current}`) : <>You have version <span className="font-mono">{version || "unknown"}</span></>}>
              {upd && !upd.up_to_date ? <Button size="sm" variant="primary" icon={<Download size={14} strokeWidth={1.5} />} onClick={apply}>Install {upd.latest}</Button>
                : <Button size="sm" variant="secondary" icon={<Github size={14} strokeWidth={1.5} />} onClick={check} disabled={checking}>{checking ? "Checking" : "Check now"}</Button>}
            </Row>
            <Row title="From a file" sub="The nightshift-app zip you were given, for a PC without internet">
              <label className="inline-flex h-8 cursor-pointer items-center gap-1.5 rounded-full border border-line-2 bg-card px-3 text-[13px] font-medium hover:bg-raised"><FileArchive size={14} strokeWidth={1.5} />Choose zip<input type="file" accept=".zip" className="sr-only" onChange={(e) => { if (e.target.files?.[0]) toast("Run the Update shortcut on your desktop and pick this zip. Installing from the browser is not available yet", "warn"); e.target.value = ""; }} /></label>
            </Row>
            <Row title="Diagnostics report" sub="A zip on your Desktop with logs and settings, without keys, to send for help">
              <Button size="sm" variant="secondary" onClick={diag}>Make report</Button>
            </Row>
          </Card>

          <Card className="px-6 py-2">
            <div className="flex items-center justify-between pt-4"><h2 className="text-[17px] font-medium">Advanced</h2>
              <Button size="sm" variant={unlocked ? "secondary" : "ghost"} icon={unlocked ? <LockOpen size={14} strokeWidth={1.5} /> : <Lock size={14} strokeWidth={1.5} />} onClick={() => setUnlocked(!unlocked)}>{unlocked ? "Lock" : "Unlock to edit"}</Button></div>
            <p className="mt-1 text-[13px] text-text-2">Engine settings for the graphics card. Profile <span className="font-mono">{s?.profile || "unknown"}</span>. Changes apply at the next engine start</p>
            <div className="mt-3 grid grid-cols-1 gap-x-6 sm:grid-cols-2">
              {Object.entries(flags).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between gap-3 border-b border-line py-2.5">
                  <label htmlFor={"flag-" + k} className="font-mono text-[12px] text-text-2">{k}</label>
                  {unlocked ? <input id={"flag-" + k} className="field-input h-8 w-[130px] font-mono text-[12px]" value={v} onChange={(e) => setFlags({ ...flags, [k]: e.target.value })} /> : <span className="font-mono text-[13px] num">{v}</span>}
                </div>
              ))}
            </div>
            {unlocked && <div className="pt-4"><Button size="sm" variant="primary" onClick={async () => { const f = Object.fromEntries(Object.entries(flags).map(([k, v]) => [k, v !== "" && !isNaN(Number(v)) ? Number(v) : v])); if (await put({ flags: f, unlock: true }, "Engine settings saved")) setUnlocked(false); }}>Save engine settings</Button></div>}
            <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-[15px] font-medium">Recent log</h3>
              <div className="flex max-w-full min-w-0 items-center gap-2"><SegTabs size="sm" label="Log level" value={level || "all"} onChange={(v) => setLevel(v === "all" ? "" : v)} items={[{ id: "all", label: "All" }, { id: "INFO", label: "Info" }, { id: "WARN", label: "Warnings" }, { id: "ERROR", label: "Errors" }]} />
                <button type="button" aria-label="Refresh log" onClick={logs.reload} className="flex size-8 items-center justify-center rounded-full text-text-2 hover:bg-raised"><RefreshCw size={14} strokeWidth={1.5} /></button></div>
            </div>
            <div className="mt-3 mb-4 max-h-[240px] overflow-auto rounded-[12px] bg-[#0b0d14] p-3 font-mono text-[12px] leading-5 text-[#c9cfdb] scroll-thin">
              {logs.data.length ? logs.data.map((l, i) => <div key={i} className="whitespace-pre"><span className="text-[#7f879a]">{l.ts}</span> <span className={l.level === "ERROR" ? "text-[#ff8a8d]" : l.level === "WARN" ? "text-[#f2b33d]" : "text-[#7f879a]"}>{l.level.padEnd(5)}</span> {l.component} {l.event} {l.msg}</div>) : <span className="text-[#7f879a]">No log lines</span>}
            </div>
          </Card>

          <Card className="px-6 py-2">
            <h2 className="pt-4 text-[17px] font-medium">About</h2>
            <Row title="Nightshift" sub={`Version ${version || "unknown"}. Runs fully offline on this PC${demo ? ". Showing demo data because no engine answered" : ""}`}><span /></Row>
            <Row title="Uninstall" sub="Run UNINSTALL.cmd in the Nightshift folder. It asks before deleting models or videos">
              <Button size="sm" variant="danger" icon={<Trash2 size={14} strokeWidth={1.5} />} onClick={() => copy(home ? `${home}\\UNINSTALL.cmd` : "UNINSTALL.cmd")}>Copy its path</Button>
            </Row>
          </Card>
        </div>
      </div>
    </div>
  );
}
