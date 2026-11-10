/* Settings: grouped cards in two columns (Schedule, Footage keys, Paths | Updates, Models, About), each a settings list
   of SettingRows with TextField, SegmentedControl, Switch and Button controls. Every call is unchanged: GET/PUT
   /settings, POST /settings/keys, GET /update/check, POST /update/apply, POST /diagnostics, GET /logs/tail. */
import { useEffect, useState } from "react";
import { useApp, useLoad } from "../lib/app";
import { api, ApiError } from "../lib/api";
import type { Job, LogLine, ModelsState, Settings as S, UpdateState } from "../lib/types";
import { dayInline, interactiveOnly, RUNS_OFF, parseReleaseNotes, plural } from "../lib/format";
import { cx } from "../ui/kit";
import { Lockup } from "../ui/Lockup";
import { Badge, Button, Icon, Card, CardHeader, DashGrid, IconButton, Input, LinkButton, ProgressBar, SegmentedControl, Select, SettingRow, SPAN, StatusDot, Switch, type SegmentOption } from "../components/ref12";

const THEMES: SegmentOption<"light" | "dark" | "soft">[] = [{ value: "light", label: "Light", icon: "sun" }, { value: "dark", label: "Dark", icon: "moon" }, { value: "soft", label: "Soft", icon: "blend" }];

export default function Settings() {
  const { toast, confirm, demo, status, theme, setTheme } = useApp();
  const st = useLoad<S | null>(() => api.get("/settings"), [], null);
  const s = st.data;
  const [time, setTime] = useState("03:00");
  const [unlocked, setUnlocked] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [flags, setFlags] = useState<Record<string, string>>({});
  const [level, setLevel] = useState("");
  const [component, setComponent] = useState("");
  const components = useLoad<string[]>(() => api.get("/logs/components"), [], []);
  const [upd, setUpd] = useState<{ current?: string; latest?: string; up_to_date?: boolean; notes?: string; state?: "up_to_date" | "available" | "unavailable" | "error" | "unknown"; message?: string } | null>(null);
  const [checking, setChecking] = useState(false);
  const logs = useLoad<LogLine[]>(() => api.get(`/logs/tail?n=200&level=${encodeURIComponent(level)}&component=${encodeURIComponent(component)}`), [level, component], []);
  // The switch follows Windows as the top bar does: the look at the task runs in the background, and what it
  // found arrives with the next status (poll or event). Loaded once, this page would keep the older state.
  useEffect(() => { if (!demo && s && status && typeof status.schedule_registered === "boolean" && (status.schedule_registered !== s.schedule_registered || status.schedule_mode !== s.schedule_mode || (status.schedule_time || s.schedule_time) !== s.schedule_time)) st.setData({ ...s, schedule_registered: status.schedule_registered, schedule_mode: status.schedule_mode, schedule_time: status.schedule_time || s.schedule_time, next_run: status.next_run ?? null }); }, [status?.schedule_registered, status?.schedule_mode, status?.schedule_time]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (s) { setTime(s.schedule_time || "03:00"); setFlags(Object.fromEntries(Object.entries(s.flags || {}).map(([k, v]) => [k, String(v)]))); } }, [s]);
  useEffect(() => { const t = setInterval(() => document.visibilityState === "visible" && logs.reload(), 5000); return () => clearInterval(t); }, [level, component]); // eslint-disable-line react-hooks/exhaustive-deps

  const put = async (body: Record<string, unknown>, ok: string) => { try { const r = await api.put("/settings", body); if (r && typeof r === "object") st.setData({ ...s, ...r }); if (r?.schedule_result && r.schedule_result.applied === false) { toast(r.schedule_result.note, "bad"); return false; } if (r?.schedule_result?.code === "NS-W205") { toast(r.schedule_result.note, "warn"); return true; } toast(ok, "ok"); return true; } catch (e: any) { toast(e.message, "bad"); return false; } };
  const setKey = async (name: string) => {
    const v = await confirm({ title: `${name[0]!.toUpperCase() + name.slice(1)} key`, body: "Paste the free key from your account page. It is saved in Windows Credential Manager and, for nightly runs that cannot reach Credential Manager, in an encrypted copy in the Nightshift folder (config\\secrets.dpapi); on a drive that keeps permissions only your Windows account and Windows itself can open that copy", ok: "Save key", input: { label: "Key", placeholder: "Paste the key" } });
    if (typeof v !== "string" || !v.trim()) return;
    try { const r = await api.post("/settings/keys", { name, value: v.trim() }); toast(r?.message || "Key saved", r?.checked === false || r?.warning ? "warn" : "ok"); st.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const check = async () => { setChecking(true); try { setUpd(await api.get("/update/check")); } catch (e: any) { toast(e.message, "bad"); } setChecking(false); };
  // The updater runs detached and stops this very service, so the POST may never be answered. What is shown comes from the
  // updater's own state file (GET /update/state), polled across the restart: "Installing", "restarting", then "Now on X (was Y)".
  const UPDATE_WAIT_MS = 10 * 60_000;
  const UPDATE_HARD_STOP_MS = 3 * 60 * 60_000; // a worker that died leaves the state on "running" for ever: stop looking after this
  const [updating, setUpdating] = useState<string | null>(null);
  // After the swap the engine starts Nightshift again (the runtime does that); this page then asks /status until the NEW version answers.
  // The 10 minutes count from the moment update_state.json stops saying "running" (or from the first lost connection when it never did),
  // not from the click: the worker downloads, installs into every venv and verifies first, which can take longer than that.
  const [updateNote, setUpdateNote] = useState<string | null>(null);
  // After an update the updater's state file also carries the new version's CHANGELOG section (release_notes); the card shows it.
  const [whatsNew, setWhatsNew] = useState<{ version: string | null; text: string } | null>(null);
  const follow = (fresh: boolean) => {
    setUpdating("Installing the update"); setUpdateNote(null); let seen = !fresh; const began = Date.now();
    let quietSince: number | null = null; let sawRunning = false;
    const t = setInterval(async () => {
      if ((quietSince !== null && Date.now() - quietSince > UPDATE_WAIT_MS) || Date.now() - began > UPDATE_HARD_STOP_MS) {
        clearInterval(t); setUpdating(null);
        setUpdateNote(sawRunning
          ? "The update has not reported back. If a window named Nightshift update is open, it shows the result and what to do. Otherwise open Nightshift from the shortcut on your desktop: the Queue page and this page then show how the update ended."
          : "Nightshift did not come back by itself. Close this window and open Nightshift again from the shortcut on your desktop. If it still does not start, make a diagnostics report from there.");
        return;
      }
      try {
        const u = await api.get<UpdateState>("/update/state");
        if (u.state === "running") { seen = true; sawRunning = true; quietSince = null; setUpdating(u.message || "Installing the update"); }
        else {
          sawRunning = false; quietSince = quietSince ?? Date.now();
          if ((u.state === "done" || u.state === "failed") && (seen || Date.now() - began > 90_000)) {
            if (u.state === "failed") {
              clearInterval(t); setUpdating(null); setUpd(null);
              // the message says what is in place ("Nothing was changed" only where that is so) and what to do
              toast(`The update did not complete. ${u.message || ""}`, "bad"); setUpdateNote(failedNote(u));
              return;
            }
            // done: the service that answers must be the new version before the page says so
            const now = await api.get<{ version?: string }>("/status");
            if (u.new && now.version && now.version !== u.new) { setUpdating("Waiting for the new version to start"); return; }
            clearInterval(t); setUpdating(null); setUpd(null);
            if (u.release_notes) setWhatsNew({ version: u.new, text: u.release_notes });
            toast(`Now on ${now.version || u.new || "the newest version"} (was ${u.old || "the old one"})`, "ok");
            // this page still holds the old version's scripts: load the new ones
            setTimeout(() => location.reload(), 2500);
          }
        }
      } catch {
        seen = true;
        // While the worker was installing the engine is stopped and nothing answers: that is not "did not come back".
        if (!sawRunning) quietSince = quietSince ?? Date.now();
        setUpdating(sawRunning ? "Nightshift is installing the update. This page reconnects by itself" : "Nightshift is restarting. This page reconnects by itself");
      }
    }, 3000);
  };
  // A failed update or rollback stays on this card until the next one: it may have ended while nobody was looking
  // (Nightshift not started again, a power cut), and its message carries the way out.
  const failedNote = (u: UpdateState) => `The last update or rollback did not complete${u.updated_at ? ` (${u.updated_at.replace("T", " ")})` : ""}. ${u.message || "See the newest update log in the logs folder."}`;
  useEffect(() => { api.get<UpdateState>("/update/state").then((u) => { if (u.state === "running") follow(false); else if (u.state === "failed") setUpdateNote(failedNote(u)); else if (u.state === "done" && u.release_notes) setWhatsNew({ version: u.new, text: u.release_notes }); }).catch(() => undefined); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const apply = async () => {
    const ok = await confirm({ title: "Install the update?", body: "Nightshift stops the engine, installs the new version and starts again. If anything fails it goes back to this version", ok: "Install" });
    if (!ok) return;
    try { await api.post("/update/apply"); }
    catch (e: any) { if (e instanceof ApiError) { toast(e.message, "bad"); return; } /* no answer: the updater has already stopped this service, which is the update starting */ }
    follow(true);
  };
  /** "From a file": the release's Nightshift-Update-<version>.zip and its .sha256, picked together. The updater refuses a zip without its checksum. */
  const installFile = async (picked: FileList | null) => {
    const files = Array.from(picked || []);
    const zip = files.find((f) => /\.zip$/i.test(f.name)); const sum = files.find((f) => /\.sha256$/i.test(f.name));
    if (!zip) { toast("Choose the file named Nightshift-Update-<version>.zip", "warn"); return; }
    if (!/^(Nightshift-Update|nightshift-app)-\d+\.\d+\.\d+.*\.zip$/i.test(zip.name)) { toast("That is not the update file. It is named Nightshift-Update-<version>.zip (the Setup zip is for a first install)", "warn"); return; }
    if (!sum) { toast(`Also choose ${zip.name}.sha256. Pick both files at once (hold Ctrl). The update is refused without it`, "warn"); return; }
    const ok = await confirm({ title: "Install this update?", body: `${zip.name} replaces the program files. Nightshift stops the engine, installs and starts again. If anything fails it goes back to this version`, ok: "Install" });
    if (!ok) return;
    const fd = new FormData(); fd.append("file", zip); fd.append("checksum", sum);
    try { await api.upload("/update/file", fd, () => {}); }
    catch (e: any) { if (!/^Upload failed/.test(String(e?.message))) { toast(e.message, "bad"); return; } /* no answer: the updater has stopped this service, which is the update starting */ }
    follow(true);
  };
  const [report, setReport] = useState<{ path?: string; name?: string; download_url?: string } | null>(null);
  const diag = async () => { try { const r = await api.post("/diagnostics"); setReport(r); toast(`Report saved to ${r?.path || "your Desktop"}`, "ok"); } catch (e: any) { toast(e.message, "bad"); } };
  const [backup, setBackup] = useState<{ path?: string; name?: string; download_url?: string; left_out?: string[]; note?: string } | null>(null);
  const exportSettings = async () => { try { const r = await api.post("/settings/export"); setBackup(r); toast(`Settings saved to ${r?.path || "your Desktop"}${r?.note ? `. ${r.note}` : ""}`, r?.left_out?.length ? "warn" : "ok"); } catch (e: any) { toast(e.message, "bad"); } };
  const importSettings = async (f: File) => {
    const ok = await confirm({ title: "Restore these settings?", body: `${f.name} replaces your channels, voices and settings. What it replaces is kept in a backup folder first`, ok: "Restore" });
    if (!ok) return;
    const fd = new FormData(); fd.append("file", f);
    try { const r = await api.upload("/settings/import", fd, () => {}); toast(r?.note || "Settings restored", r?.left_out?.length ? "warn" : "ok"); st.reload(); } catch (e: any) { toast(e.message, "bad"); }
  };
  const models = useLoad<ModelsState>(() => api.get("/models/state"), [], {});
  // Where UNINSTALL.cmd really is (<install folder>\app), so the row names the folder instead of "the Nightshift folder".
  const uninstallInfo = useLoad<{ path?: string } | null>(() => api.get("/settings/uninstall"), [], null);
  const uninstallDir = uninstallInfo.data?.path ? uninstallInfo.data.path.replace(/[\\/][^\\/]*$/, "") : null;
  const busyModel = Object.values(models.data).some((m) => m.state === "running");
  useEffect(() => { if (!busyModel) return; const t = setInterval(models.reload, 4000); return () => clearInterval(t); }, [busyModel]); // eslint-disable-line react-hooks/exhaustive-deps
  const getModel = async (name: string, label: string) => {
    try { await api.post("/models/get", { name }); toast(`Downloading the ${label}. It takes a while; you can keep working`, "ok"); models.reload(); }
    catch (e: any) { toast(e instanceof ApiError && e.code === "models_unavailable" ? `Getting the ${label} from here is not available yet. Run the installer's model step again with it ticked` : e.message, e instanceof ApiError && e.code === "models_unavailable" ? "warn" : "bad"); }
  };
  const [warming, setWarming] = useState(false);
  const warmUp = async () => { setWarming(true); try { await api.post("/engine/warmup", { service: "llama" }); toast("The language model is starting. It is ready in about a minute", "ok"); } catch (e: any) { toast(e.message, "bad"); } setWarming(false); };
  const stopAll = async () => {
    const ok = await confirm({ title: "Stop everything?", body: "This stops the models and cancels the job that is running. Videos already finished stay in the Queue", ok: "Stop all", danger: true });
    if (!ok) return; try { await api.post("/engine/stop_all"); toast("Everything is stopped", "ok"); } catch (e: any) { toast(e.message, "bad"); }
  };
  const selfCheck = useLoad<Job[]>(() => api.get("/jobs?kind=verify&limit=1"), [], []);
  const lastCheck = selfCheck.data[0]; const checkBusy = lastCheck?.status === "queued" || lastCheck?.status === "running";
  useEffect(() => { if (!checkBusy) return; const t = setInterval(selfCheck.reload, 2500); return () => clearInterval(t); }, [checkBusy]); // eslint-disable-line react-hooks/exhaustive-deps
  const runCheck = async (quick: boolean) => { try { await api.post("/jobs/verify", { quick }); toast(quick ? "Quick self-check started" : "Full self-check started. It takes several minutes", "ok"); selfCheck.reload(); } catch (e: any) { toast(e.message, "bad"); } };
  const res = lastCheck?.result as { passed?: boolean; checks?: { passed: boolean }[]; warnings?: string[] } | null | undefined;
  const bad = res?.checks?.filter((c) => !c.passed).length ?? 0;
  const checkSub = !lastCheck ? "Checks the models, the voice and the video encoder, and writes a report" : checkBusy ? `Running. ${lastCheck.message || ""}` : lastCheck.status === "failed" ? `The check could not run. ${lastCheck.message || ""}` : lastCheck.status === "cancelled" ? "Cancelled" : res?.passed ? `Passed ${dayInline(lastCheck.created_at)}. ${res.warnings?.length ? `One thing to look at: ${res.warnings.join(" ")}` : "Everything answered"}` : `${plural(bad, "item")} need attention. The report is in the logs folder`;
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
            {/* The switch shows what Windows has (schedule_registered), never the click: put() takes the answer. */}
            <SettingRow title="Automatic runs" sub={!s ? "" : s.dev_mode ? "Not available in a dev install: its nightly task is registered disabled and never runs (docs/DEV_MODE.md)" : s.schedule_registered ? `On. Nightshift wakes this PC every night at ${s.schedule_time} and makes the videos of your active channels` : `Off. ${RUNS_OFF}. Switching on adds the task 'Nightshift Daily' to Windows`}>
              <Switch checked={!!s?.schedule_registered} disabled={!s || !!s.dev_mode || switching} onChange={async (v) => { setSwitching(true); await put({ automatic_runs: v, ...(v ? { schedule_time: time } : {}) }, v ? `Automatic runs are on: every night at ${time}` : "Automatic runs are off. Nothing runs by itself"); setSwitching(false); }} label="Automatic runs" />
            </SettingRow>
            <SettingRow title="Run time" sub={s?.schedule_registered ? "Nightshift wakes the PC at this time and works while you sleep" : "Kept for when you switch Automatic runs on"}>
              <Input aria-label="Run time" type="time" className="!w-[132px] font-mono" value={time} onChange={(e) => setTime(e.target.value)} />
              <Button disabled={time === s?.schedule_time} onClick={() => put({ schedule_time: time }, s?.schedule_registered ? `Runs at ${time} from tonight` : `${time} saved. Automatic runs are off`)}>Save</Button>
            </SettingRow>
            {s && s.schedule_mode === "interactive_only" && (
              <SettingRow title={interactiveOnly(s.schedule_time)} sub="Windows would not run the task while you are signed out (NS-W205). Lock the screen instead of signing out, or the night's run will not start. Try again asks Windows once more; if it refuses again, run 'ns schedule install' once from an elevated PowerShell">
                <Button variant="soft" leadingIcon="refresh" onClick={() => put({ automatic_runs: true, schedule_time: s.schedule_time }, `Windows accepted it: the run at ${s.schedule_time} now starts whether you are signed in or not`)}>Try again</Button>
              </SettingRow>
            )}
            <SettingRow title="Notifications" sub="A Windows notification when a run finishes or stops">
              <Switch checked={!!s?.notifications} onChange={(v) => { st.setData(s ? { ...s, notifications: v } : s); put({ notifications: v }, v ? "Notifications on" : "Notifications off"); }} label="Notifications" />
            </SettingRow>
            <SettingRow title="Theme" sub="Saved in this browser">
              <SegmentedControl label="Theme" value={theme} onChange={setTheme} options={THEMES} />
            </SettingRow>
          </Card>

          <Card as="section" aria-labelledby="s-keys">
            <CardHeader id="s-keys" title="Stock footage keys" className="!mb-1" />
            {["pexels", "pixabay"].map((k) => (
              <SettingRow key={k} title={k[0]!.toUpperCase() + k.slice(1)} sub={<>{s?.keys?.[k] ? <span className="font-mono">••••••••••••••••</span> : "Missing. Stories may run short of footage"}{s?.key_warnings?.[k] && <span role="status" className="mt-1 block text-ds-danger">{s.key_warnings[k].code}: {s.key_warnings[k].message} {s.key_warnings[k].hint}</span>}</>}>
                <Button size="sm" variant={s?.keys?.[k] ? "soft" : "primary"} leadingIcon="key" onClick={() => setKey(k)}>{s?.keys?.[k] ? "Replace" : "Add key"}</Button>
              </SettingRow>
            ))}
            {Object.entries(s?.key_warnings || {}).map(([k, w]) => (
              <p key={`w-${k}`} role="alert" className="mt-2 text-sm text-danger" data-testid={`key-warning-${k}`}>
                <span className="font-mono">{w.code}</span> {k[0]!.toUpperCase() + k.slice(1)}: the copy of this key that nightly runs read was not saved or locked. {w.hint}
              </p>
            ))}
          </Card>

          <Card as="section" aria-labelledby="s-backup">
            <CardHeader id="s-backup" title="Your settings" className="!mb-1" />
            <SettingRow title="Export settings" sub={backup?.path ? <>Saved as <span className="font-mono break-all">{backup.path}</span>{backup.note && <span role="status" className="mt-1 block text-ds-danger">{backup.note}</span>}</> : "A zip with your channels, voices, schedule and learned weights. Models and videos are not included"}>
              <Button variant="soft" leadingIcon="archive" onClick={exportSettings}>Export</Button>
              {backup?.download_url && <LinkButton href={backup.download_url} download={backup.name} variant="soft" leadingIcon="download">Download</LinkButton>}
            </SettingRow>
            <SettingRow title="Import settings" sub="Restore a zip made by Export settings, for example on a new PC. What it replaces is kept in a backup folder first">
              <label className="ds-focus inline-flex h-9 cursor-pointer items-center gap-2 rounded-ds-md bg-ds-accent-soft px-4 text-ds-sm font-semibold shadow-ds-control hover:bg-ds-soft-hover focus-within:shadow-ds-focus">
                <Icon name="upload" size={15} />
                Choose zip<input type="file" accept=".zip" className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) importSettings(f); }} />
              </label>
            </SettingRow>
          </Card>

          <Card as="section" aria-labelledby="s-paths">
            <CardHeader id="s-paths" title="Paths" className="!mb-1" />
            {[["Everything", home], ["Videos waiting for you", home && `${home}\\outbox`], ["Models", home && `${home}\\models`], ["Logs", home && `${home}\\logs`]].map(([l, p]) => (
              <SettingRow key={l} title={l!} sub={<span className="font-mono text-ds-2xs break-all">{p || "Unknown until the engine answers"}</span>}>{p && <Button size="sm" variant="ghost" leadingIcon="copy" onClick={() => copy(p)}>Copy</Button>}</SettingRow>
            ))}
            {disk && disk.total_gb != null && disk.free_gb != null && (
              <SettingRow title="Free space" sub={`On the drive that holds ${home}`}>
                <div className="flex w-56 flex-col gap-1"><span className="text-right text-ds-xs font-semibold ds-num">{disk.free_gb} GB of {disk.total_gb} GB free</span><ProgressBar showValue={false} value={((disk.total_gb - disk.free_gb) / Math.max(1, disk.total_gb)) * 100} label="Disk used" /></div>
              </SettingRow>
            )}
          </Card>
        </div>

        <div className={cx("flex flex-col gap-5", SPAN["3/4"])}>
          <Card as="section" aria-labelledby="s-upd">
            <CardHeader id="s-upd" title="Updates" className="!mb-1" adornment={upd?.state === "available" ? <Badge tone="accent">New version</Badge> : undefined} />
            <SettingRow title="From GitHub" sub={upd ? (upd.state === "unavailable" || upd.state === "error" || upd.state === "unknown" ? upd.message : upd.up_to_date ? `You have the newest version${upd.current ? `, ${upd.current}` : ""}` : `Version ${upd.latest} is ready. You have ${upd.current}`) : <>You have version <span className="font-mono">{version || "unknown"}</span></>}>
              {updating ? <span className="flex items-center gap-2 text-ds-sm font-semibold text-ds-accent-ink" role="status"><StatusDot tone="accent" />{updating}</span>
                : upd?.state === "available" ? <Button leadingIcon="download" onClick={apply}>Install {upd.latest}</Button>
                : <Button variant="soft" leadingIcon="github" onClick={check} disabled={checking}>{checking ? "Checking" : "Check now"}</Button>}
            </SettingRow>
            {whatsNew && (
              <section aria-labelledby="s-whatsnew" className="mx-1 mb-2 max-h-72 overflow-y-auto rounded-ds-md bg-ds-accent-soft px-3 py-3 text-ds-sm">
                <h3 id="s-whatsnew" className="mb-1 font-semibold">What is new in {whatsNew.version || "this version"}</h3>
                {parseReleaseNotes(whatsNew.text).map((b, i) => b.kind === "heading" ? <h4 key={i} className="mt-2 font-semibold">{b.text}</h4>
                  : b.kind === "list" ? <ul key={i} className="mt-1 list-disc space-y-1 pl-5">{b.items.map((it, j) => <li key={j}>{it}</li>)}</ul>
                  : <p key={i} className="mt-1">{b.text}</p>)}
              </section>
            )}
            {updateNote && <p role="alert" className="mx-1 mb-2 rounded-ds-md bg-ds-danger-soft px-3 py-2 text-ds-sm">{updateNote}</p>}
            <SettingRow title="From a file" sub="A Nightshift-Update zip you were given (some updates still need the internet). Choose Nightshift-Update-<version>.zip and its .sha256 file together (hold Ctrl); the update is refused without the checksum file">
              <label className="ds-focus inline-flex h-9 cursor-pointer items-center gap-2 rounded-ds-md bg-ds-accent-soft px-4 text-ds-sm font-semibold shadow-ds-control hover:bg-ds-soft-hover focus-within:shadow-ds-focus">
                <Icon name="archive" size={15} />
                Choose files<input type="file" multiple accept=".zip,.sha256" className="sr-only" onChange={(e) => { installFile(e.target.files); e.target.value = ""; }} />
              </label>
            </SettingRow>
            <SettingRow title="Diagnostics report" sub={report?.path ? <>Saved as <span className="font-mono break-all">{report.path}</span></> : "A zip on your Desktop with logs and settings, without keys, to send for help"}>
              <Button variant="soft" leadingIcon="archive" onClick={diag}>Make report</Button>
              {report?.download_url && <LinkButton href={report.download_url} download={report.name} variant="soft" leadingIcon="download">Download</LinkButton>}
            </SettingRow>
          </Card>

          <Card as="section" aria-labelledby="s-engine">
            <CardHeader id="s-engine" title="Engine" className="!mb-1" adornment={status?.engine ? <StatusDot tone={status.engine.state === "error" ? "danger" : status.engine.state === "idle" ? "attention" : "online"} label={status.engine.detail || status.engine.state} /> : undefined} />
            <SettingRow title="Warm up" sub="Loads the language model now, so the first job of the day starts fast">
              <Button variant="soft" leadingIcon="power" disabled={warming} onClick={warmUp}>{warming ? "Starting" : "Warm up"}</Button>
            </SettingRow>
            <SettingRow title="Stop everything" sub="Stops the models and cancels the job that is running. Use it when the PC feels stuck">
              <Button variant="danger" leadingIcon="x" onClick={stopAll}>Stop all</Button>
            </SettingRow>
            <SettingRow title="Self-check" sub={checkSub}>
              <Button variant="soft" leadingIcon="checklist" disabled={checkBusy} onClick={() => runCheck(true)}>Quick check</Button>
              <Button variant="ghost" disabled={checkBusy} onClick={() => runCheck(false)}>Full check</Button>
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
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {([["voicedesign", "voice design model"], ["story_llm", "story model"]] as const).map(([name, label]) => (
                <Button key={name} size="sm" variant="soft" leadingIcon="download" disabled={models.data[name]?.state === "running"} onClick={() => getModel(name, label)}>
                  {models.data[name]?.state === "running" ? `Getting the ${label}` : `Get ${label}`}
                </Button>
              ))}
              {Object.entries(models.data).filter(([, m]) => m.state !== "running").map(([k, m]) => <span key={k} className={cx("text-ds-2xs", m.state === "failed" ? "text-ds-danger" : "text-ds-success-ink")}>{k}: {m.state === "done" ? "ready" : m.message || "failed"}</span>)}
            </div>
            {unlocked && <div className="pt-4"><Button onClick={async () => { const f = Object.fromEntries(Object.entries(flags).map(([k, v]) => [k, v !== "" && !isNaN(Number(v)) ? Number(v) : v])); if (await put({ flags: f, unlock: true }, "Engine settings saved")) setUnlocked(false); }}>Save engine settings</Button></div>}
            <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
              <h3 className="text-ds-md font-semibold">Recent log</h3>
              <div className="flex max-w-full min-w-0 items-center gap-2">
                <SegmentedControl size="sm" label="Log level" value={level || "all"} onChange={(v) => setLevel(v === "all" ? "" : v)} options={[{ value: "all", label: "All" }, { value: "INFO", label: "Info" }, { value: "WARN", label: "Warnings" }, { value: "ERROR", label: "Errors" }]} />
                <Select aria-label="Log component" className="!h-8 !w-auto !text-ds-xs" value={component} onChange={(e) => setComponent(e.target.value)}><option value="">All parts</option>{components.data.map((c) => <option key={c} value={c}>{c}</option>)}</Select>
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
            <SettingRow title="Uninstall" sub={`Run UNINSTALL.cmd in ${uninstallDir ?? "the app folder inside your Nightshift install folder"}. It asks before deleting models or videos`}>
              <Button size="sm" variant="danger" leadingIcon="trash" onClick={async () => { try { const r = await api.get("/settings/uninstall"); await copy(r.path); } catch { await copy("UNINSTALL.cmd"); } }}>Copy its path</Button>
            </SettingRow>
          </Card>
        </div>
      </DashGrid>
    </div>
  );
}
