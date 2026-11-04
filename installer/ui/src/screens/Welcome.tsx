// 2.1 Welcome, and its resume variant.
import { useEffect, useRef, useState } from "react";
import { Button, Card, CardHeader, ClipsIllustration, DottedRule, FeatureCard, MetaItem, StoryIllustration, VoiceIllustration } from "@ref12";
import { cx } from "@dash/ui/kit";
import { welcome as t, stepTitle } from "../copy";
import { estimateFor, gb } from "../format";
import { useLiveState } from "../api/useSetup";
import type { DownloadRow, FieldError, PlanGroup, Tier } from "../api/types";
import { Area, Screen } from "../parts/Screen";
import { LabeledField } from "../parts/LabeledField";
import { CopyStatusButton } from "../parts/CopyStatus";

const TIER_NAME: Record<Tier, string> = { small: "Small", balanced: "Balanced", best: "Best" };

interface PlanLine { id: string; label: string; size: string; muted?: boolean }

function planLines(groups: PlanGroup[], rows: DownloadRow[] | null): PlanLine[] {
  const lines: PlanLine[] = [];
  for (const g of groups) {
    if (rows) {
      const left = rows.filter((r) => r.group === g.id && r.status !== "done" && r.status !== "skipped");
      if (left.length === 0) continue;
      const bytes = left.reduce((a, r) => a + Math.max(0, (r.bytesTotal ?? 0) - r.bytesDone), 0);
      lines.push(bytes > 0 ? { id: g.id, label: g.label, size: gb(bytes) } : { id: g.id, label: g.label, size: left.map((r) => r.label).join(", "), muted: true });
      continue;
    }
    if (g.bytes != null && g.bytes > 0) lines.push({ id: g.id, label: g.label, size: g.items.every((i) => i.bytes == null) ? t.sizeApprox(gb(g.bytes)) : gb(g.bytes) });
    else if (g.items.some((i) => i.bytes == null) && g.items.length > 0) lines.push({ id: g.id, label: g.label, size: g.items.map((i) => i.label).join(", "), muted: true });
    else lines.push({ id: g.id, label: g.label, size: t.sizeUnknown, muted: true });
  }
  return lines;
}

function Advanced({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { api, state, toast, act } = useLiveState();
  const ref = useRef<HTMLDivElement>(null);
  const [path, setPath] = useState("");
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!ref.current?.contains(e.target as Node) && !(e.target as HTMLElement).closest("[data-advanced-toggle]")) onClose(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("pointerdown", away); window.addEventListener("keydown", esc, true);
    ref.current?.querySelector("input")?.focus({ preventScroll: true });
    return () => { window.removeEventListener("pointerdown", away); window.removeEventListener("keydown", esc, true); };
  }, [open, onClose]);

  const load = async () => {
    setLoading(true); setErrors([]);
    try {
      const r = await api.loadAnswersFile(path.trim());
      if (r.ok) { toast(t.drawer.loaded); onClose(); } else setErrors(r.errors);
    } catch (e) { toast(e instanceof Error ? e.message : t.drawer.loaded); } finally { setLoading(false); }
  };
  const logs = `${state.answers?.homePath ?? state.machine.meters.disk.path}\\logs`;
  return (
    <div ref={ref} role="dialog" aria-label={t.drawer.title} aria-hidden={!open} {...(open ? {} : ({ inert: "" } as object))}
      className={cx("ns-drawer absolute top-[132px] right-8 bottom-[72px] z-30 w-[min(580px,calc(50%-24px))] transition-[transform,opacity] duration-200 ease-ds-out", open ? "translate-x-0 opacity-100" : "pointer-events-none translate-x-8 opacity-0")}>
      <Card elevation={3} radius="2xl" padding="lg" className="flex h-full flex-col gap-6 overflow-y-auto">
        <CardHeader title={t.drawer.title} className="mb-0" actions={<Button variant="ghost" size="sm" onClick={onClose}>{t.drawer.close}</Button>} />
        <section className="flex flex-col gap-3">
          <h3 className="text-ds-lg font-semibold">{t.drawer.answersTitle}</h3>
          <form className="flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); if (path.trim()) void load(); }}>
            <LabeledField label={t.drawer.answersLabel} value={path} onChange={(e) => setPath(e.target.value)} placeholder={t.drawer.answersPlaceholder} className="flex-1" inputClassName="font-mono text-ds-xs" />
            <Button type="submit" variant="soft" size="md" disabled={!path.trim() || loading}>{t.drawer.load}</Button>
          </form>
          {errors.length > 0 && <ul className="flex flex-col gap-1 text-ds-xs text-ds-danger">{errors.map((e) => <li key={e.field}><span className="font-mono">{e.field}</span> {e.message}</li>)}</ul>}
        </section>
        <section className="flex flex-col gap-3">
          <h3 className="text-ds-lg font-semibold">{t.drawer.logsTitle}</h3>
          <div><Button variant="ghost" size="md" leadingIcon="folder" className="-ml-2" onClick={() => void act("reveal", { path: logs })}>{t.drawer.logsButton}</Button></div>
        </section>
        <section className="flex flex-col gap-2">
          <h3 className="text-ds-lg font-semibold">{t.drawer.headlessTitle}</h3>
          <p className="text-ds-sm leading-ds-normal text-ds-text-2">{t.drawer.headless}</p>
        </section>
      </Card>
    </div>
  );
}

export function Welcome({ resume }: { resume: boolean }) {
  const { state, act, setUiStep, busy } = useLiveState();
  const [open, setOpen] = useState(false);
  // Resume variant: the checks run again in the background; the Checks screen only interrupts when a row blocks.
  useEffect(() => { if (resume && state.run === "idle" && !state.checking) void act("recheck"); // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resume]);
  const rows = resume ? state.downloads.items : null;
  const lines = planLines(state.plan.groups, rows);
  const left = rows ? Math.max(0, state.downloads.overall.bytesTotal - state.downloads.overall.bytesDone) : state.plan.totalBytes;
  const est = rows ? estimateFor(left) : state.plan.estimate;
  const stopped = state.steps.find((s) => s.status === "running" || s.status === "pending");
  const stepName = stopped ? stopped.title || stepTitle[stopped.id] : stepTitle.s03_download_models;
  const start = () => (resume ? void act(state.run === "idle" ? "start" : "resume") : void setUiStep("checks"));

  return (
    <>
      <Screen name="welcome" salutation={resume ? t.resumeSalutation : t.salutation} question={t.question} description={resume ? t.resumeDescription(stepName) : t.description}
        footerLeft={state.prefilledFrom && !resume ? t.footerPrefilled(state.prefilledFrom) : t.footer(state.version, state.appPath)}
        footerRight={
          <>
            {resume && <CopyStatusButton />}
            <Button variant="soft" size="lg" data-advanced-toggle onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="dialog">{t.advanced}</Button>
            <Button variant="primary" size="lg" trailingIcon="chevronRight" data-primary disabled={busy.has("start") || busy.has("resume") || busy.has("ui-step:checks")} onClick={start}>{resume ? t.resume : t.start}</Button>
          </>
        }>
        <Area area="f1"><FeatureCard className="[&_.line-clamp-2]:line-clamp-3" illustration={<StoryIllustration />} title={t.tiles[0]!.title} description={t.tiles[0]!.description} /></Area>
        <Area area="f2"><FeatureCard className="[&_.line-clamp-2]:line-clamp-3" illustration={<ClipsIllustration />} title={t.tiles[1]!.title} description={t.tiles[1]!.description} /></Area>
        <Area area="f3"><FeatureCard className="[&_.line-clamp-2]:line-clamp-3" illustration={<VoiceIllustration />} title={t.tiles[2]!.title} description={t.tiles[2]!.description} /></Area>
        <Area area="time">
          <Card elevation={0} tone="sunken" padding="md" radius="lg" className="flex flex-col justify-center gap-1.5">
            <MetaItem icon="clock" className="text-ds-md font-semibold text-ds-text">{est.label}</MetaItem>
            <p className="text-ds-xs leading-ds-normal text-ds-text-2">{t.timeExplain}</p>
          </Card>
        </Area>
        <Area area="plan">
          <Card as="section" elevation={1} padding="lg" aria-label={resume ? t.planTitleResume : t.planTitle} className="flex flex-col">
            <CardHeader title={resume ? t.planTitleResume : t.planTitle} level={2} />
            <dl className="flex flex-col gap-2.5">
              {lines.map((l) => (
                <div key={l.id} className="flex items-baseline justify-between gap-4 text-ds-sm">
                  <dt>{l.label}</dt>
                  <dd className={cx("min-w-0 text-right", l.muted ? "text-ds-xs text-ds-muted" : "ds-num")}>{l.size}</dd>
                </div>
              ))}
            </dl>
            <div className="mt-auto flex pt-4 pb-4"><DottedRule /></div>
            <dl className="flex flex-col gap-2.5">
              <div className="flex items-baseline justify-between gap-4 text-ds-sm"><dt>{t.totals.model}</dt><dd className="ds-num">{TIER_NAME[state.plan.tier]}{state.plan.tier === state.plan.recommendedTier ? ` (${t.recommended})` : ""}</dd></div>
              <div className="flex items-baseline justify-between gap-4 text-ds-sm"><dt className="font-medium">{t.totals.download}</dt><dd className="ds-num font-semibold">{gb(left)}</dd></div>
              <div className="flex items-baseline justify-between gap-4 text-ds-sm"><dt>{t.totals.free}</dt><dd className="ds-num">{state.plan.needFreeGb} GB</dd></div>
              <div className="flex items-baseline justify-between gap-4 text-ds-sm"><dt>{t.totals.time}</dt><dd className="ds-num">{est.label.replace("About ", "about ")}</dd></div>
            </dl>
          </Card>
        </Area>
      </Screen>
      <Advanced open={open} onClose={() => setOpen(false)} />
    </>
  );
}
