/* Settings: grouped cards in two columns (Schedule, Footage keys, Paths | Updates, Models, About), each a settings list
   of SettingRows with TextField, SegmentedControl, Switch and Button controls. Every call is unchanged: GET/PUT
   /settings, POST /settings/keys, GET /update/check, POST /update/apply, POST /diagnostics, GET /logs/tail. */
import { useEffect, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api } from "../lib/api";
import type { LogLine, Settings as S } from "../lib/types";
import { cx } from "../ui/kit";
import { Lockup } from "../ui/Lockup";
import { Badge, Button, Icon, Card, CardHeader, DashGrid, IconButton, Input, ProgressBar, SegmentedControl, SettingRow, SPAN, Switch, type SegmentOption } from "../components/ref12";

const THEMES: SegmentOption<"light" | "dark" | "soft">[] = [{ value: "light", label: "Light", icon: "sun" }, { value: "dark", label: "Dark", icon: "moon" }, { value: "soft", label: "Soft", icon: "blend" }];

export default function Settings() {
  const { toast, confirm, demo, status, theme, setTheme } = useApp();
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
    const v = await confirm({ title: `${name[0]!.toUpperCase() + name.slice(1)} key`, body: "Paste the free key from your account page. It is stored in Windows Credential Manager, never in a file", ok: "Save key", input: { label: "Key", placeholder: "Paste the key" } });
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
  const disk = status?.disk;

  return (
    <div className="enter flex flex-col gap-5">
      <p className="text-ds-md text-ds-text-2">The defaults from setup work. Change things here only if you want to</p>
      <DashGrid label="Settings">
        <div className={cx("flex flex-col gap-5", SPAN["3/4"])}>
          <Card as="section" aria-labelledby="s-sched">
            <CardHeader id="s-sched" title="Schedule" className="!mb-1" />
            <SettingRow title="Automatic run time" sub="Nightshift wakes the PC at this time and works while you sleep">
              <Input aria-label="Run time" type="time" className="!w-[132px] font-mono" value={time} onChange={(e) => setTime(e.target.value)} />
              <Button disabled={time === s?.schedule_time} onClick={() => put({ schedule_time: time }, `Runs at ${time} from tonight`)}>Save</Button>
            </SettingRow>
            <SettingRow title="Notifications" sub="A Windows notification when a run finishes or stops">
              <Switch checked={!!s?.notifications} onChange={(v) => { st.setData({ ...s, notifications: v }); put({ notifications: v }, v ? "Notifications on" : "Notifications off"); }} label="Notifications" />
            </SettingRow>
            <SettingRow title="Theme" sub="Saved in this browser">
              <SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEMES} />
            </SettingRow>
          </Card>

          <Card as="section" aria-labelledby="s-keys">
            <CardHeader id="s-keys" title="Stock footage keys" className="!mb-1" />
            {["pexels", "pixabay"].map((k) => (
              <SettingRow key={k} title={k[0]!.toUpperCase() + k.slice(1)} sub={s?.keys?.[k] ? <span className="font-mono">••••••••••••••••</span> : "Missing. Stories may run short of footage"}>
                <Button size="sm" variant={s?.keys?.[k] ? "soft" : "primary"} leadingIcon="key" onClick={() => setKey(k)}>{s?.keys?.[k] ? "Replace" : "Add key"}</Button>
              </SettingRow>
            ))}
          </Card>

          <Card as="section" aria-labelledby="s-paths">
            <CardHeader id="s-paths" title="Paths" className="!mb-1" />
            {[["Everything", home], ["Videos waiting for you", home && `${home}\\outbox`], ["Models", home && `${home}\\models`], ["Logs", home && `${home}\\logs`]].map(([l, p]) => (
              <SettingRow key={l} title={l!} sub={<span className="font-mono text-ds-2xs break-all">{p || "Unknown until the engine answers"}</span>}>{p && <Button size="sm" variant="ghost" leadingIcon="copy" onClick={() => copy(p)}>Copy</Button>}</SettingRow>
            ))}
            {disk && (
              <SettingRow title="Free space" sub={`On the drive that holds ${home}`}>
                <div className="flex w-56 flex-col gap-1"><span className="text-right text-ds-xs font-semibold ds-num">{disk.free_gb} GB of {disk.total_gb} GB free</span><ProgressBar showValue={false} value={((disk.total_gb - disk.free_gb) / Math.max(1, disk.total_gb)) * 100} label="Disk used" /></div>
              </SettingRow>
            )}
          </Card>
        </div>

        <div className={cx("flex flex-col gap-5", SPAN["3/4"])}>
          <Card as="section" aria-labelledby="s-upd">
            <CardHeader id="s-upd" title="Updates" className="!mb-1" adornment={upd && !upd.up_to_date ? <Badge tone="accent">New version</Badge> : undefined} />
            <SettingRow title="From GitHub" sub={upd ? (upd.up_to_date ? `You have the newest version, ${upd.current}` : `Version ${upd.latest} is ready. You have ${upd.current}`) : <>You have version <span className="font-mono">{version || "unknown"}</span></>}>
              {upd && !upd.up_to_date ? <Button leadingIcon="download" onClick={apply}>Install {upd.latest}</Button>
                : <Button variant="soft" leadingIcon="github" onClick={check} disabled={checking}>{checking ? "Checking" : "Check now"}</Button>}
            </SettingRow>
            <SettingRow title="From a file" sub="The nightshift-app zip you were given, for a PC without internet">
              <label className="ds-focus inline-flex h-9 cursor-pointer items-center gap-2 rounded-ds-md bg-ds-accent-soft px-4 text-ds-sm font-semibold shadow-ds-control hover:bg-ds-soft-hover focus-within:shadow-ds-focus">
                <Icon name="archive" size={15} />
                Choose zip<input type="file" accept=".zip" className="sr-only" onChange={(e) => { if (e.target.files?.[0]) toast("Run the Update shortcut on your desktop and pick this zip. Installing from the browser is not available yet", "warn"); e.target.value = ""; }} />
              </label>
            </SettingRow>
            <SettingRow title="Diagnostics report" sub="A zip on your Desktop with logs and settings, without keys, to send for help">
              <Button variant="soft" leadingIcon="archive" onClick={diag}>Make report</Button>
            </SettingRow>
          </Card>

          <Card as="section" aria-labelledby="s-models">
            <CardHeader id="s-models" title="Models" className="!mb-1" actions={<Button size="sm" variant={unlocked ? "soft" : "ghost"} leadingIcon={unlocked ? "lockOpen" : "lock"} onClick={() => setUnlocked(!unlocked)}>{unlocked ? "Lock" : "Unlock to edit"}</Button>} />
            <p className="text-ds-xs leading-ds-normal text-ds-text-2">Engine settings for the graphics card. Profile <span className="font-mono">{s?.profile || "unknown"}</span>. Changes apply at the next engine start</p>
            <div className="mt-3 grid grid-cols-1 gap-x-6 sm:grid-cols-2">
              {Object.entries(flags).map(([k, v]) => (
                <div key={k} className="flex items-center justify-between gap-3 border-b border-ds-border-subtle py-2.5">
                  <label htmlFor={"flag-" + k} className="font-mono text-ds-2xs text-ds-text-2">{k}</label>
                  {unlocked ? <Input id={"flag-" + k} className="!h-8 !w-[130px] font-mono !text-ds-xs" value={v} onChange={(e) => setFlags({ ...flags, [k]: e.target.value })} /> : <span className="font-mono text-ds-xs ds-num">{v}</span>}
                </div>
              ))}
            </div>
            {unlocked && <div className="pt-4"><Button onClick={async () => { const f = Object.fromEntries(Object.entries(flags).map(([k, v]) => [k, v !== "" && !isNaN(Number(v)) ? Number(v) : v])); if (await put({ flags: f, unlock: true }, "Engine settings saved")) setUnlocked(false); }}>Save engine settings</Button></div>}
            <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-ds-md font-semibold">Recent log</h3>
              <div className="flex max-w-full min-w-0 items-center gap-2">
                <SegmentedControl size="sm" label="Log level" value={level || "all"} onChange={(v) => setLevel(v === "all" ? "" : v)} options={[{ value: "all", label: "All" }, { value: "INFO", label: "Info" }, { value: "WARN", label: "Warnings" }, { value: "ERROR", label: "Errors" }]} />
                <IconButton icon="refresh" label="Refresh log" size="sm" onClick={logs.reload} />
              </div>
            </div>
            <div className="mt-3 max-h-[240px] overflow-auto rounded-ds-lg bg-ds-media p-3 font-mono text-ds-2xs leading-5 text-white/80 scroll-thin" tabIndex={0} aria-label="Recent log lines">
              {logs.data.length ? logs.data.map((l, i) => <div key={i} className="whitespace-pre"><span className="text-white/45">{l.ts}</span> <span className={l.level === "ERROR" ? "text-[#f07d92]" : l.level === "WARN" ? "text-[#f6b545]" : "text-white/45"}>{l.level.padEnd(5)}</span> {l.component} {l.event} {l.msg}</div>) : <span className="text-white/45">No log lines</span>}
            </div>
          </Card>

          <Card as="section" aria-labelledby="s-about">
            <CardHeader id="s-about" title="About" className="!mb-1" />
            <div className="flex justify-center py-3"><Lockup width={200} /></div>
            <SettingRow title="Nightshift" sub={`Version ${version || "unknown"}. Runs fully offline on this PC${demo ? ". Showing demo data because no engine answered" : ""}`} />
            <SettingRow title="Uninstall" sub="Run UNINSTALL.cmd in the Nightshift folder. It asks before deleting models or videos">
              <Button size="sm" variant="danger" leadingIcon="trash" onClick={() => copy(home ? `${home}\\UNINSTALL.cmd` : "UNINSTALL.cmd")}>Copy its path</Button>
            </SettingRow>
          </Card>
        </div>
      </DashGrid>
    </div>
  );
}
