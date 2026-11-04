// 2.2 Hardware check.
import { useState } from "react";
import { Button, Card, SegmentedControl } from "@ref12";
import { checks as t, codeText, common } from "../copy";
import { hhmm } from "../format";
import { useLiveState } from "../api/useSetup";
import type { CheckRow, ChoicesDraft } from "../api/types";
import { openExternal } from "../host";
import { Area, Screen } from "../parts/Screen";
import { CheckRowItem } from "../parts/RowItems";
import { IssueCard } from "../parts/IssueCard";
import { NeedMeters, type NeedMeter } from "../parts/Widgets";

function tone(have: number, need: number, hardMin: number): NeedMeter["tone"] {
  if (have >= need) return "accent";
  return have < hardMin ? "danger" : "highlight";
}

/** The full draft the server needs to re-check the disk rows for another install folder. */
export function draftFor(state: ReturnType<typeof useLiveState>["state"], homePath: string): ChoicesDraft {
  const a = state.answers;
  return {
    ownerName: a?.ownerName ?? null, channel: a?.channel ?? { displayName: "", niche: "", language: "en" }, homePath, llmTier: a?.llmTier ?? state.plan.tier, llmCustom: a?.llmCustom ?? null,
    voice: a?.voice ?? { speaker: "Ryan" }, modelsSource: a?.modelsSource ?? { kind: "internet", path: null }, runTime: a?.runTime ?? "03:00", mastersPerWeek: a?.mastersPerWeek ?? 1,
    installCode: a?.installCode ?? true, includeVoicedesign: a?.includeVoicedesign ?? false, theme: a?.theme ?? (document.documentElement.dataset.theme === "light" ? "light" : "dark"), pexelsKey: null, pixabayKey: null,
  };
}

function DrivePicker({ row }: { row: CheckRow }) {
  const { state, api, toast } = useLiveState();
  const need = state.machine.meters.disk.hardMinGb;
  const drives = state.machine.drives.filter((d) => d.freeGb >= need);
  const current = state.machine.meters.disk.path[0]?.toUpperCase();
  const [pick, setPick] = useState<string>(current ?? "");
  if (drives.length === 0) return <p className="text-ds-sm text-ds-text-2">{t.noDrive(need)}</p>;
  const choose = async (letter: string) => {
    setPick(letter);
    try { await api.postAnswers(draftFor(state, `${letter}:\\Nightshift`)); } catch (e) { toast(e instanceof Error ? e.message : "Setup did not answer"); }
  };
  return (
    <div id={`drive-${row.id}`}>
      <SegmentedControl label={t.driveLabel} variant="plain" options={drives.map((d) => ({ value: d.letter, label: t.driveOption(d.letter, d.freeGb) }))} value={pick} onChange={(v) => void choose(v)} className="flex-wrap" />
    </div>
  );
}

function Fix({ row, picking, onPick }: { row: CheckRow; picking: boolean; onPick: () => void }) {
  const { act, busy } = useLiveState();
  const fix = row.fix;
  if (!fix) return null;
  const variant = row.status === "fail" ? "primary" : "soft";
  switch (fix.kind) {
    case "open-url": return <Button variant={variant} size="md" trailingIcon="external" onClick={() => openExternal(fix.url)}>{fix.label}</Button>;
    case "elevate-longpaths": return <Button variant={variant} size="md" disabled={busy.has("elevate-longpaths")} onClick={() => void act("elevate-longpaths")}>{fix.label}</Button>;
    case "recheck": return <Button variant={variant} size="md" leadingIcon="restore" disabled={busy.has("recheck")} onClick={() => void act("recheck")}>{fix.label}</Button>;
    case "choose-drive": return picking ? null : <Button variant={variant} size="md" onClick={onPick}>{fix.label}</Button>;
  }
}

export function Checks() {
  const { state, act, setUiStep, busy } = useLiveState();
  const [picking, setPicking] = useState<Record<string, boolean>>({});
  const rows = state.checks;
  const ok = rows.filter((r) => r.status === "ok").length;
  const warn = rows.filter((r) => r.status === "warn").length;
  const fail = rows.filter((r) => r.status === "fail").length;
  const issues = [...rows.filter((r) => r.status === "fail"), ...rows.filter((r) => r.status === "warn")];
  const m = state.machine.meters;
  const all: NeedMeter[] = [
    { label: t.meterLabels.vram, value: Math.min(100, Math.round((m.vram.haveGb / m.vram.needGb) * 100)), tone: tone(m.vram.haveGb, m.vram.needGb, m.vram.hardMinGb), caption: t.meterCaption(m.vram.haveGb, m.vram.needGb) },
    { label: t.meterLabels.ram, value: Math.min(100, Math.round((m.ram.haveGb / m.ram.needGb) * 100)), tone: tone(m.ram.haveGb, m.ram.needGb, m.ram.hardMinGb), caption: t.meterCaption(m.ram.haveGb, m.ram.needGb) },
    { label: t.meterLabels.disk, value: Math.min(100, Math.round((m.disk.haveGb / m.disk.needGb) * 100)), tone: tone(m.disk.haveGb, m.disk.needGb, m.disk.hardMinGb), caption: t.meterCaption(m.disk.haveGb, m.disk.needGb) },
  ];
  // a dev install needs no graphics card: its video memory meter would only show a shortfall that does not matter
  const meters = state.devMode ? all.slice(1) : all;
  const jump = (r: CheckRow) => {
    const el = document.getElementById(`issue-${r.id}`);
    el?.scrollIntoView({ block: "nearest", behavior: "auto" });
    (el?.querySelector("button, a") as HTMLElement | null)?.focus();
  };
  const checking = state.checking;
  return (
    <Screen name="checks" salutation={t.salutation} description={t.summary(ok, rows.length, warn, fail)}
      headerRight={<Button variant="soft" size="md" leadingIcon="restore" disabled={checking || busy.has("recheck")} onClick={() => void act("recheck")}>{checking ? t.checking : t.checkAgain}</Button>}
      footerLeft={state.blocking ? <span className="text-ds-danger">{t.footerBlocking}</span> : state.checkedAt ? t.footerChecked(hhmm(state.checkedAt)) : t.footerNever}
      footerRight={
        <>
          <Button variant="soft" size="lg" onClick={() => void setUiStep("welcome")}>{t.back}</Button>
          <Button variant="primary" size="lg" data-primary disabled={state.blocking || checking} onClick={() => void setUiStep("choices")}>{t.continue}</Button>
        </>
      }>
      <Area area="rows">
        <Card as="section" padding="md" aria-label={t.salutation}>
          <ul className="flex flex-col gap-1" aria-busy={checking}>
            {rows.map((r) => <CheckRowItem key={r.id} row={r} checking={checking} onActivate={() => jump(r)} />)}
          </ul>
        </Card>
      </Area>
      <Area area="meters"><NeedMeters title={t.metersTitle} meters={meters} /></Area>
      <Area area="cards" className="ns-cards scroll-thin overflow-y-auto">
        <div className="flex flex-col gap-3 pb-1">
          {issues.length === 0 && <Card elevation={0} tone="sunken" padding="md" radius="lg" className="text-ds-sm text-ds-text-2">{t.noCards}</Card>}
          {issues.map((r) => {
            const copy = r.code ? codeText(r.code) : null;
            const title = r.title ?? copy?.title ?? r.label;
            const what = r.whatToDo ?? copy?.whatToDo ?? common.issueWhatToDo;
            const isPicking = !!picking[r.id] && r.fix?.kind === "choose-drive";
            return (
              <IssueCard key={r.id} id={`issue-${r.id}`} tone={r.status === "fail" ? "fail" : "warn"} code={r.code} title={title} whatToDo={what}
                action={<Fix row={r} picking={isPicking} onPick={() => setPicking((p) => ({ ...p, [r.id]: true }))} />}>
                {isPicking && <DrivePicker row={r} />}
              </IssueCard>
            );
          })}
        </div>
      </Area>
    </Screen>
  );
}
