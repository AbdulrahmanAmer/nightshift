// 2.3 Choices: everything s01_questions asks, on one screen. Validated here for instant feedback with the rules of the spec;
// the server's FieldError[] stays authoritative.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Card, CardHeader, SegmentedControl, TimeChip } from "@ref12";
import { cx } from "@dash/ui/kit";
import { choices as t } from "../copy";
import { gbNum, parseRunTime } from "../format";
import { useLiveState } from "../api/useSetup";
import type { ChoicesDraft, ChoicesField, DiskCheck, PresetVoice, Tier } from "../api/types";
import { openExternal } from "../host";
import { Area, Screen } from "../parts/Screen";
import { ChoiceRow } from "../parts/ChoiceRow";
import { LabeledField } from "../parts/LabeledField";
import { SetupIcon } from "../parts/icons";

const VOICES: PresetVoice[] = ["Ryan", "Aiden", "Serena", "Vivian"];
const RUN_PRESETS = ["01:00", "03:00", "05:00"];
const TIER_NAME: Record<Tier, string> = { small: "Small", balanced: "Balanced", best: "Best" };

type KeyStatus = { status: "idle" | "checking" | "ok" | "bad"; message?: string; checked?: string };
type Errors = Partial<Record<ChoicesField, string>>;

function useDismiss(open: boolean, close: () => void, ref: React.RefObject<HTMLElement>) {
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
    window.addEventListener("pointerdown", away); window.addEventListener("keydown", esc, true);
    return () => { window.removeEventListener("pointerdown", away); window.removeEventListener("keydown", esc, true); };
  }, [open, close, ref]);
}

export function Choices() {
  const { state, api, act, setUiStep, toast, busy } = useLiveState();
  const initial = useMemo<ChoicesDraft>(() => {
    const a = state.answers;
    const theme = (document.documentElement.dataset.theme === "light" ? "light" : "dark") as "dark" | "light";
    return a
      ? { ownerName: a.ownerName, channel: a.channel, homePath: a.homePath, llmTier: a.llmTier, voice: a.voice, modelsSource: a.modelsSource, runTime: a.runTime, mastersPerWeek: a.mastersPerWeek, installCode: a.installCode, theme: a.theme ?? theme, pexelsKey: null, pixabayKey: null }
      : { ownerName: null, channel: { displayName: "", niche: "", language: "en" }, homePath: state.machine.meters.disk.path, llmTier: state.plan.recommendedTier, voice: { speaker: "Ryan" }, modelsSource: { kind: "internet", path: null }, runTime: "03:00", mastersPerWeek: 1, installCode: true, theme, pexelsKey: null, pixabayKey: null };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [d, setD] = useState<ChoicesDraft>(initial);
  const [otherTime, setOtherTime] = useState(() => !RUN_PRESETS.includes(initial.runTime));
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState(false);
  const [serverErrors, setServerErrors] = useState<Errors>({});
  const [keys, setKeys] = useState<{ pexels: KeyStatus; pixabay: KeyStatus }>({ pexels: { status: "idle" }, pixabay: { status: "idle" } });
  const [disk, setDisk] = useState<DiskCheck | null>(null);
  const [diskBusy, setDiskBusy] = useState(false);
  const [folder, setFolder] = useState<DiskCheck | null>(null);
  const [more, setMore] = useState(false);
  const [keyPop, setKeyPop] = useState(false);
  const [saving, setSaving] = useState(false);
  const lastPosted = useRef<string>("");
  const moreRef = useRef<HTMLDivElement>(null);
  const keyRef = useRef<HTMLDivElement>(null);
  const locked = state.answersLocked;
  const set = useCallback(<K extends keyof ChoicesDraft>(k: K, v: ChoicesDraft[K]) => { setD((p) => ({ ...p, [k]: v })); setServerErrors((e) => { const n = { ...e }; delete n[k as ChoicesField]; return n; }); }, []);
  const touch = (f: string) => setTouched((p) => (p.has(f) ? p : new Set(p).add(f)));
  useDismiss(more, () => setMore(false), moreRef);
  useDismiss(keyPop, () => setKeyPop(false), keyRef);

  // The install folder: checked 400 ms after the last keystroke.
  useEffect(() => {
    setDiskBusy(true);
    let live = true;
    const id = window.setTimeout(() => { api.disk(d.homePath, d.llmTier).then((r) => { if (live) { setDisk(r); setDiskBusy(false); } }).catch(() => live && setDiskBusy(false)); }, 400);
    return () => { live = false; window.clearTimeout(id); };
  }, [api, d.homePath, d.llmTier]);
  // The models folder (folder or Drive zip).
  useEffect(() => {
    if (d.modelsSource.kind === "internet" || !d.modelsSource.path?.trim()) { setFolder(null); return; }
    let live = true;
    const id = window.setTimeout(() => { api.disk(d.modelsSource.path ?? "", d.llmTier, true).then((r) => live && setFolder(r)).catch(() => undefined); }, 400);
    return () => { live = false; window.clearTimeout(id); };
  }, [api, d.modelsSource.kind, d.modelsSource.path, d.llmTier]);

  const tierInfo = state.plan.tiers[d.llmTier];
  const ram = state.machine.ramGb;
  const keySet = { pexels: state.answers?.pexelsKeySet ?? false, pixabay: state.answers?.pixabayKeySet ?? false };

  const errors: Errors = useMemo(() => {
    const e: Errors = {};
    const name = d.channel.displayName.trim();
    if (name.length < 1 || name.length > 60) e["channel.displayName"] = t.errors.displayName;
    const niche = d.channel.niche.trim();
    if (niche.length < 2 || niche.length > 60) e["channel.niche"] = t.errors.niche;
    if ((d.ownerName ?? "").trim().length > 40) e.ownerName = t.errors.ownerName;
    const p = d.homePath.trim();
    if (!/^[A-Za-z]:\\/.test(p) || p.includes("%") || p.length > 80) e.homePath = t.errors.homePathShape;
    else if (disk && !diskBusy && !disk.ok) e.homePath = t.errors.homePath(tierInfo.needFreeGb, Math.round(disk.freeGb ?? 0));
    if (!tierInfo.allowed) e.llmTier = t.errors.llmTier(TIER_NAME[d.llmTier], ram);
    if (d.modelsSource.kind !== "internet" && (!d.modelsSource.path?.trim() || (folder && !folder.exists))) e["modelsSource.path"] = t.errors.folder;
    if (!parseRunTime(d.runTime)) e.runTime = t.errors.runTime;
    if (!d.pexelsKey?.trim() && !keySet.pexels) e.pexelsKey = t.errors.key("Pexels");
    if (!d.pixabayKey?.trim() && !keySet.pixabay) e.pixabayKey = t.errors.key("Pixabay");
    return e;
  }, [d, disk, diskBusy, folder, tierInfo, ram, keySet.pexels, keySet.pixabay]);

  const keyBlocking = (k: "pexels" | "pixabay") => {
    const val = (k === "pexels" ? d.pexelsKey : d.pixabayKey)?.trim();
    return !!val && keys[k].status !== "ok";
  };
  const valid = Object.keys(errors).length === 0 && !keyBlocking("pexels") && !keyBlocking("pixabay") && !diskBusy;
  const shown = (f: ChoicesField): string | undefined => serverErrors[f] ?? (showAll || touched.has(f) ? errors[f] : undefined);

  const checkKey = async (k: "pexels" | "pixabay") => {
    const val = (k === "pexels" ? d.pexelsKey : d.pixabayKey)?.trim() ?? "";
    touch(k === "pexels" ? "pexelsKey" : "pixabayKey");
    if (!val) { setKeys((p) => ({ ...p, [k]: { status: "idle" } })); return; }
    if (keys[k].checked === val && keys[k].status !== "checking") return;
    setKeys((p) => ({ ...p, [k]: { status: "checking", checked: val } }));
    try {
      const r = await api.checkKey(k, val);
      setKeys((p) => (p[k].checked === val ? { ...p, [k]: r.ok ? { status: "ok", checked: val } : { status: "bad", message: r.message ?? "The key was not accepted", checked: val } } : p));
    } catch { setKeys((p) => ({ ...p, [k]: { status: "bad", message: "Setup could not check the key", checked: val } })); }
  };

  const install = async () => {
    setShowAll(true);
    if (!valid) { (document.querySelector("[aria-invalid=true]") as HTMLElement | null)?.focus(); return; }
    const draft: ChoicesDraft = {
      ...d, ownerName: d.ownerName?.trim() ? d.ownerName.trim() : null, channel: { ...d.channel, displayName: d.channel.displayName.trim(), niche: d.channel.niche.trim() },
      homePath: d.homePath.trim(), pexelsKey: d.pexelsKey?.trim() || null, pixabayKey: d.pixabayKey?.trim() || null,
    };
    setSaving(true);
    try {
      const sig = JSON.stringify(draft);
      if (sig !== lastPosted.current) {
        const r = await api.postAnswers(draft);
        if (!r.ok) {
          const map: Errors = {}; for (const e of r.errors) map[e.field] = e.message;
          setServerErrors(map); (document.querySelector("[aria-invalid=true]") as HTMLElement | null)?.focus();
          return;
        }
        lastPosted.current = sig;
      }
      await act("start");
    } catch (e) { toast(e instanceof Error ? e.message : t.saveFailed); } finally { setSaving(false); }
  };

  const [hh, mm] = parseRunTime(d.runTime) ?? [null, null];
  const folderLabel = d.modelsSource.kind === "drive-zip" ? t.source.zipLabel : t.source.folderLabel;
  const freeText = disk && disk.freeGb != null ? `${Math.round(disk.freeGb)} GB` : null;

  const KeyField = ({ k }: { k: "pexels" | "pixabay" }) => {
    const st = keys[k]; const field = k === "pexels" ? "pexelsKey" : "pixabayKey"; const val = (k === "pexels" ? d.pexelsKey : d.pixabayKey) ?? "";
    const err = st.status === "bad" ? st.message : shown(field);
    return (
      <LabeledField inline label={k === "pexels" ? t.keys.pexels : t.keys.pixabay} type="password" autoComplete="off" spellCheck={false} value={val} disabled={locked}
        placeholder={keySet[k] ? "••••••••" : ""} hint={keySet[k] && !val ? t.keys.pexelsKept : undefined} error={err}
        onChange={(e) => { const v = e.target.value; set(field, v); setKeys((p) => ({ ...p, [k]: { status: "idle" } })); }} onBlur={() => void checkKey(k)}
        trailing={st.status === "ok" ? <span className="flex shrink-0 items-center gap-1 text-ds-xs text-ds-success-ink"><SetupIcon name="check" size={14} />{t.keys.works}</span> : st.status === "checking" ? <span className="shrink-0 text-ds-xs text-ds-text-2">{t.keys.checking}</span> : undefined} />
    );
  };

  return (
    <Screen name="choices" salutation={t.salutation} description={t.description}
      footerLeft={
        <div className="flex items-center gap-3">
          <span className="truncate" title={d.homePath}>{diskBusy && !freeText ? t.footer.checking : t.footer.folder(d.homePath, freeText)}</span>
          <div ref={moreRef} className="relative">
            <Button variant="ghost" size="sm" aria-expanded={more} aria-haspopup="dialog" onClick={() => setMore((o) => !o)}>{t.footer.more}</Button>
            {more && (
              <Card elevation={3} padding="lg" radius="xl" role="dialog" aria-label={t.more.title} className="ns-pop absolute! bottom-full left-0 z-30 mb-3 flex w-[480px] flex-col gap-4 text-ds-text">
                <h3 className="text-ds-lg font-semibold">{t.more.title}</h3>
                <div className="flex flex-col gap-2">
                  <LabeledField label={t.more.folder} value={d.homePath} disabled={locked} onChange={(e) => set("homePath", e.target.value)} onBlur={() => touch("homePath")} error={shown("homePath")} inputClassName="font-mono text-ds-xs" />
                  {disk?.message && !errors.homePath && <p className="text-ds-xs text-ds-text-2 ds-num">{disk.message}</p>}
                  <SegmentedControl label={t.more.drive} variant="plain" size="sm" value={(d.homePath[0] ?? "").toUpperCase()} onChange={(v) => set("homePath", `${v}:\\Nightshift`)}
                    options={state.machine.drives.map((x) => ({ value: x.letter, label: `${x.letter}:` }))} />
                </div>
                <div className="flex flex-col gap-1.5">
                  <span className="text-ds-sm font-medium text-ds-text-2">{t.more.perWeek}</span>
                  <SegmentedControl label={t.more.perWeek} value={String(d.mastersPerWeek)} onChange={(v) => set("mastersPerWeek", Number(v) as 1 | 2 | 3)} options={[{ value: "1", label: "1" }, { value: "2", label: "2" }, { value: "3", label: "3" }]} />
                  <span className="text-ds-xs text-ds-muted">{t.more.perWeekCaption}</span>
                </div>
                <ChoiceRow type="checkbox" checked={d.installCode} onChange={(v) => set("installCode", v)} label={t.more.code} className="-mx-3" />
                <div className="flex flex-col gap-1.5">
                  <span className="text-ds-sm font-medium text-ds-text-2">{t.more.theme}</span>
                  <SegmentedControl label={t.more.theme} value={d.theme} onChange={(v) => { document.documentElement.dataset.theme = v; set("theme", v); }}
                    options={[{ value: "dark", label: t.more.dark, icon: "moon" }, { value: "light", label: t.more.light, icon: "sun" }]} />
                </div>
              </Card>
            )}
          </div>
        </div>
      }
      footerRight={
        <>
          <Button variant="soft" size="lg" onClick={() => void setUiStep("checks")}>{t.back}</Button>
          <Button variant="primary" size="lg" data-primary disabled={!valid || saving || busy.has("start")} onClick={() => void install()}>{t.install}</Button>
        </>
      }>
      <Area area="chan">
        <Card as="section" padding="lg" aria-label={t.channel.title}>
          <CardHeader title={t.channel.title} level={2} />
          <div className="flex flex-col gap-3">
            <LabeledField inline label={t.channel.name} required maxLength={60} placeholder={t.channel.namePh} value={d.channel.displayName} disabled={locked} error={shown("channel.displayName")}
              onChange={(e) => set("channel", { ...d.channel, displayName: e.target.value })} onBlur={() => touch("channel.displayName")} />
            <LabeledField inline label={t.channel.about} required maxLength={60} placeholder={t.channel.aboutPh} value={d.channel.niche} disabled={locked} error={shown("channel.niche")}
              onChange={(e) => set("channel", { ...d.channel, niche: e.target.value })} onBlur={() => touch("channel.niche")} />
            <LabeledField inline label={t.channel.owner} maxLength={40} placeholder={t.channel.ownerPh} value={d.ownerName ?? ""} disabled={locked} error={shown("ownerName")}
              onChange={(e) => set("ownerName", e.target.value)} onBlur={() => touch("ownerName")} />
          </div>
        </Card>
      </Area>
      <Area area="tier">
        <Card as="section" padding="lg" aria-label={t.tier.title}>
          <CardHeader title={t.tier.title} level={2} />
          <SegmentedControl label={t.tier.label} variant="filled" size="md" value={d.llmTier} onChange={(v) => { set("llmTier", v); touch("llmTier"); }} options={t.tier.options} />
          <div className="mt-4 flex flex-col gap-1 text-ds-sm ds-num">
            <p className="font-medium">{t.tier.line1[d.llmTier](gbNum(tierInfo.llmBytes))}</p>
            <p className="text-ds-text-2">{t.tier.line2[d.llmTier]}</p>
            <p className="text-ds-muted">{t.tier.line3(gbNum(tierInfo.totalBytes), tierInfo.needFreeGb)}</p>
          </div>
          {state.plan.recommendedTier === "small" && ram < 31 && <p className="mt-3 text-ds-xs leading-ds-normal text-ds-text-2">{t.tier.lowRam(ram)}</p>}
          {!tierInfo.allowed && <Badge tone="danger" size="sm" className="mt-3">{tierInfo.reason ?? t.tier.notAllowedFallback}</Badge>}
        </Card>
      </Area>
      <Area area="run">
        <Card as="section" padding="lg" aria-label={t.run.title}>
          <CardHeader title={t.run.title} level={2} />
          <p className="flex gap-3" aria-label={`${d.runTime}`}>
            <TimeChip value={hh ?? "--"} unit={t.run.hours} /><TimeChip value={mm ?? "--"} unit={t.run.minutes} />
          </p>
          <div className="mt-4">
            <SegmentedControl label={t.run.label} variant="filled" value={otherTime ? "other" : d.runTime} onChange={(v) => { if (v === "other") setOtherTime(true); else { setOtherTime(false); set("runTime", v); } }}
              options={[...RUN_PRESETS.map((v) => ({ value: v, label: v })), { value: "other", label: t.run.other }]} />
          </div>
          {otherTime && <LabeledField className="mt-3" label={t.run.otherLabel} inputMode="numeric" pattern="([01]\d|2[0-3]):[0-5]\d" placeholder="03:00" value={d.runTime} disabled={locked} error={shown("runTime")} onChange={(e) => set("runTime", e.target.value)} onBlur={() => touch("runTime")} inputClassName="w-28 font-mono" />}
          <p className="mt-3 text-ds-xs leading-ds-normal text-ds-muted">{t.run.caption}</p>
        </Card>
      </Area>
      <Area area="voice">
        <Card as="section" padding="lg" aria-label={t.voice.title}>
          <CardHeader title={t.voice.title} level={2} />
          <SegmentedControl label={t.voice.label} variant="plain" value={d.voice.speaker} onChange={(v) => set("voice", { speaker: v })} options={VOICES.map((v) => ({ value: v, label: v }))} />
          <p className="mt-3 text-ds-sm font-medium">{t.voice.lines[d.voice.speaker]}</p>
          <p className="mt-1 text-ds-xs leading-ds-normal text-ds-muted">{t.voice.caption}</p>
        </Card>
      </Area>
      <Area area="src">
        <Card as="section" padding="lg" aria-label={t.source.title}>
          <CardHeader title={t.source.title} level={2} />
          <div role="radiogroup" aria-label={t.source.label} className="-mx-3 flex flex-col gap-0.5">
            {([["internet", t.source.internet], ["folder", t.source.folder], ["drive-zip", t.source.driveZip]] as const).map(([kind, label]) => (
              <ChoiceRow key={kind} type="radio" name="models-source" checked={d.modelsSource.kind === kind} disabled={locked} onChange={() => set("modelsSource", { kind, path: kind === "internet" ? null : d.modelsSource.path })} label={label} />
            ))}
          </div>
          {d.modelsSource.kind !== "internet" && (
            <div className="mt-2">
              <LabeledField label={folderLabel} placeholder={d.modelsSource.kind === "drive-zip" ? t.source.zipPh : t.source.folderPh} value={d.modelsSource.path ?? ""} disabled={locked} error={shown("modelsSource.path")}
                onChange={(e) => set("modelsSource", { ...d.modelsSource, path: e.target.value })} onBlur={() => touch("modelsSource.path")} inputClassName="font-mono text-ds-xs" />
              {folder?.modelsFound && folder.exists && <p className="mt-1.5 text-ds-xs text-ds-text-2 ds-num">{t.source.found(folder.modelsFound.files, folder.modelsFound.of, gbNum(folder.modelsFound.bytes))}</p>}
            </div>
          )}
        </Card>
      </Area>
      <Area area="keys">
        <Card as="section" padding="lg" aria-label={t.keys.title}>
          <CardHeader title={t.keys.title} level={2} />
          <div className="flex flex-col gap-3">
            {KeyField({ k: "pexels" })}
            {KeyField({ k: "pixabay" })}
          </div>
          <div ref={keyRef} className="relative mt-3">
            <Button variant="ghost" size="sm" trailingIcon="external" className="-ml-2" aria-expanded={keyPop} aria-haspopup="dialog" onClick={() => setKeyPop((o) => !o)}>{t.keys.getKeys}</Button>
            {keyPop && (
              <Card elevation={3} padding="md" radius="lg" role="dialog" aria-label={t.keys.popoverLabel} className={cx("ns-pop absolute! bottom-full left-0 z-30 mb-2 flex w-60 flex-col gap-1")}>
                <Button variant="ghost" size="md" trailingIcon="external" className="justify-between" onClick={() => openExternal("https://www.pexels.com/api/")}>{t.keys.pexelsLink}</Button>
                <Button variant="ghost" size="md" trailingIcon="external" className="justify-between" onClick={() => openExternal("https://pixabay.com/api/docs/")}>{t.keys.pixabayLink}</Button>
              </Card>
            )}
          </div>
        </Card>
      </Area>
    </Screen>
  );
}
