// 2.8 Failed: shown over any phase when a step failed. Every error says what to do next.
import { useState } from "react";
import { Badge, Button, Card, CardHeader } from "@ref12";
import { codeText, failed as t, stepTitle } from "../copy";
import { hhmm } from "../format";
import { useLiveState } from "../api/useSetup";
import type { FailureView } from "../api/types";
import { Area, Screen } from "../parts/Screen";
import { StepItem } from "../parts/RowItems";
import { SetupIcon } from "../parts/icons";
import { CopyStatusButton } from "../parts/CopyStatus";

export function Failed({ onStop }: { onStop: () => void }) {
  const { state, act, toast, busy } = useLiveState();
  const [all, setAll] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const f: FailureView = state.failure ?? {
    step: state.steps.find((s) => s.status === "failed")?.id ?? "s03_download_models", stepTitle: "", code: "unexpected", title: codeText("unexpected").title,
    whatToDo: codeText("unexpected").whatToDo, detail: "", canSkip: false, attempts: 1, at: state.updatedAt,
  };
  const stepName = f.stepTitle || stepTitle[f.step] || t.unknownStep;
  const lines = f.detail.split("\n");
  const long = lines.length > 6 || f.detail.length > 420;
  const failedStep = state.steps.find((s) => s.id === f.step);
  const who = state.support.name ?? t.whoFallback;
  const BS = "\\";
  const logs = state.answers?.homePath ? `${state.answers.homePath.replace(/\\+$/, "")}${BS}logs` : null;
  const saveHelp = async () => {
    const r = await act("diagnostics");
    if (!r.ok || !r.message) return;
    const name = r.message.split(/[\\/]/).pop() ?? r.message;
    setSaved(name);
    toast(t.helpAfter(name, who));
  };
  return (
    <Screen name="failed" salutation={t.salutation(stepName)} description={t.description}
      footerLeft={t.footer(hhmm(f.at))}
      footerRight={
        <>
          <CopyStatusButton />
          <Button variant="ghost" size="lg" onClick={onStop}>{t.stop}</Button>
          {f.canSkip && <Button variant="soft" size="lg" disabled={busy.has("skip")} onClick={() => void act("skip")}>{t.skip}</Button>}
          <Button variant="primary" size="lg" leadingIcon="restore" data-primary disabled={busy.has("retry")} onClick={() => void act("retry")}>{t.retry}</Button>
        </>
      }>
      <Area area="err">
        <Card as="section" elevation={2} padding="lg" aria-label={f.title} className="flex flex-col gap-5 overflow-y-auto scroll-thin">
          <div className="flex items-center gap-3">
            <SetupIcon name="error" size={22} className="shrink-0 text-ds-danger" />
            <Badge tone="danger" variant="soft" size="md" className="ds-num">{f.code}</Badge>
            <h2 className="min-w-0 text-ds-xl leading-ds-tight font-semibold">{f.title}</h2>
          </div>
          <div>
            <CardHeader title={t.whatToDo} level={3} className="mb-2" />
            <p className="max-w-[60ch] text-ds-md leading-ds-normal">{f.whatToDo}</p>
          </div>
          {(f.detail || f.attempts > 0) && (
            <div>
              <CardHeader title={t.details} level={3} className="mb-2" />
              {f.detail && <pre className={`font-mono text-ds-xs leading-ds-normal break-all whitespace-pre-wrap text-ds-text-2 ${all ? "" : "line-clamp-6"}`}>{f.detail}</pre>}
              {long && <Button variant="ghost" size="sm" className="-ml-2 mt-1" onClick={() => setAll((a) => !a)}>{all ? t.showLess : t.showAll}</Button>}
              <p className="mt-2 text-ds-xs text-ds-text-2 ds-num">{t.tried(f.attempts)}</p>
            </div>
          )}
          <div className="flex flex-col gap-3">
            <p className="max-w-[60ch] text-ds-sm leading-ds-normal text-ds-text-2">{saved ? t.helpAfter(saved, who) : t.helpBefore(who)}</p>
            <div className="flex flex-wrap items-center gap-3">
              {f.code === "NS-E113" && <Button variant="primary" size="md" trailingIcon="external" onClick={() => void act("open-windows-page", { path: "protection-history" })}>{t.openProtection}</Button>}
              <Button variant="soft" size="md" leadingIcon="copy" disabled={busy.has("diagnostics")} onClick={() => void saveHelp()}>{t.saveHelp}</Button>
              {logs && <Button variant="soft" size="md" leadingIcon="folder" onClick={() => void act("reveal", { path: logs })}>{t.openLogs}</Button>}
            </div>
          </div>
        </Card>
      </Area>
      <Area area="steps">
        <Card as="section" padding="md" aria-label={t.stepsTitle} className="overflow-y-auto scroll-thin">
          <CardHeader title={t.stepsTitle} level={2} className="mb-2 px-2" />
          <ul className="flex flex-col gap-0.5">{state.steps.map((s) => <StepItem key={s.id} step={s} compact current={s.id === failedStep?.id} />)}</ul>
        </Card>
      </Area>
    </Screen>
  );
}
