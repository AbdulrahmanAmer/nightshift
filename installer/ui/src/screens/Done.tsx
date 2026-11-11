// 2.7 Done: three numbered next steps, then where the videos land and what to send back if something looks wrong.
import { useEffect, useState } from "react";
import { Button, Card, CardHeader, DottedRule, StatusDot } from "@ref12";
import { done as t } from "../copy";
import { duration } from "../format";
import { useLiveState } from "../api/useSetup";
import { openExternal } from "../host";
import { Area, Screen } from "../parts/Screen";
import { CopyStatusButton } from "../parts/CopyStatus";
import { useCopyReport } from "../parts/useCopyReport";

export function Done() {
  const { state, act, toast } = useLiveState();
  const { copy, dialog } = useCopyReport();
  const [opening, setOpening] = useState(false);
  useEffect(() => { if (!opening) return; const id = window.setTimeout(() => setOpening(false), 3000); return () => window.clearTimeout(id); }, [opening]);
  const r = state.result;
  if (!r) return null;
  const zip = r.diagnosticsZip;
  const zipName = zip ? zip.split(/[\/]/).pop() : null;
  const who = state.support.name ?? t.whoFallback;
  const open = async () => { setOpening(true); await act("open-dashboard"); };
  // The warnings keep their instructions: the title says what is wrong, "what to do" says what to do about it.
  const warnings = [...r.warnings.map((w) => ({ key: w.code + w.title, title: w.title, what: w.whatToDo })), ...(state.machine.isAdmin ? [{ key: "admin", title: t.adminNote, what: "" }] : [])];
  // Automatic runs: on, off by choice (the default), or failed (setup tried to switch them on and Windows refused).
  const nightly = r.nightly ?? (r.scheduled ? "on" : "failed");
  const steps = [t.steps[0], t.steps[1], nightly === "on" ? t.steps[2] : nightly === "off" ? t.stepsOff : nightly === "unrecorded" ? t.stepsUnrecorded : t.stepsNoTask];
  return (
    <Screen name="done" salutation={t.salutation} description={nightly === "on" ? t.description(r.runTime) : nightly === "off" ? t.descriptionOff : nightly === "unrecorded" ? t.descriptionUnrecorded(r.runTime) : t.descriptionNoTask}
      footerLeft={opening ? t.opening : t.footer(duration(r.durationS))}
      footerRight={
        <>
          <CopyStatusButton />
          <Button variant="soft" size="lg" leadingIcon="copy" onClick={() => void copy()}>{t.copyReport}</Button>
          <Button variant="primary" size="lg" data-primary disabled={opening} onClick={() => void open()}>{t.openDashboard}</Button>
        </>
      }>
      <Area area="inst">
        <Card as="section" padding="lg" aria-label={t.stepsTitle} className="overflow-y-auto scroll-thin">
          <CardHeader title={t.stepsTitle} level={2} />
          <ol className="flex flex-col gap-3 text-ds-sm leading-ds-normal">
            {steps.map((s, i) => (
              <li key={s} className="flex gap-3"><span className="grid size-5 shrink-0 place-items-center rounded-full bg-ds-sunken text-ds-xs font-semibold ds-num">{i + 1}</span><span className="min-w-0">{s}</span></li>
            ))}
          </ol>
          {r.nightlyLine && <p className="mt-4 text-ds-xs leading-ds-normal text-ds-text-2">{r.nightlyLine}</p>}
          {r.shortcutsLine && <p className="mt-2 text-ds-xs leading-ds-normal text-ds-text-2">{r.shortcutsLine}</p>}
          {r.homeVarLine && <p className="mt-2 text-ds-xs leading-ds-normal text-ds-text-2">{r.homeVarLine}</p>}
          {r.commandsLine && <p className="mt-2 text-ds-xs leading-ds-normal text-ds-text-2">{r.commandsLine}</p>}
          {r.stockKeyMissing && <p className="mt-4 flex items-center gap-2 text-ds-sm font-medium"><StatusDot tone="attention" size={7} />{t.keyReminder}</p>}
          {warnings.length > 0 && (
            <ul className="mt-4 flex flex-col gap-2.5">
              {warnings.map((w) => (
                <li key={w.key} className="flex items-start gap-2 text-ds-xs leading-ds-normal text-ds-text-2">
                  <span className="mt-1 shrink-0"><StatusDot tone="attention" size={5} /></span>
                  <span className="min-w-0"><span className="font-medium text-ds-text">{w.title}</span>{w.what ? <> {w.what}</> : null}</span>
                </li>
              ))}
            </ul>
          )}
          <details className="mt-4 text-ds-sm">
            <summary className="cursor-pointer text-ds-text-2">{t.installedTitle}</summary>
            <dl className="mt-3 flex flex-col gap-2.5">
              {r.installed.map((i) => (
                <div key={i.label} className="flex items-baseline justify-between gap-4">
                  <dt className="min-w-0">{i.label}</dt>
                  <dd className="max-w-[55%] shrink-0 truncate text-right ds-num" title={i.detail}>{i.detail}</dd>
                </div>
              ))}
            </dl>
          </details>
        </Card>
      </Area>
      <Area area="out">
        <Card as="section" padding="lg" aria-label={t.outTitle}>
          <CardHeader title={t.outTitle} level={2} />
          <p className="font-mono text-ds-sm break-all">{r.outbox}</p>
          <div className="mt-3"><Button variant="soft" size="md" leadingIcon="folder" onClick={() => void act("reveal", { path: r.outbox })}>{t.openFolder}</Button></div>
          <div className="my-4 flex"><DottedRule /></div>
          <h3 className="text-ds-sm font-semibold">{t.shortcuts}</h3>
          <ul className="mt-2 flex flex-col gap-1 text-ds-sm text-ds-text-2">{r.shortcuts.map((s) => <li key={s}>{s}</li>)}</ul>
        </Card>
      </Area>
      <Area area="send">
        <Card as="section" padding="lg" aria-label={t.sendTitle}>
          <CardHeader title={t.sendTitle} level={2} />
          {zip ? (
            <>
              <p className="font-mono text-ds-sm break-all" title={zip}>{zipName}</p>
              <p className="mt-2 text-ds-sm leading-ds-normal text-ds-text-2">{t.sendBody(who)}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button variant="soft" size="md" leadingIcon="folder" onClick={() => void act("reveal", { path: zip })}>{t.showFile}</Button>
                {state.support.driveUrl && <Button variant="soft" size="md" trailingIcon="external" onClick={() => openExternal(state.support.driveUrl!)}>{t.openDrive}</Button>}
              </div>
            </>
          ) : (
            <>
              <p className="text-ds-sm leading-ds-normal text-ds-text-2">{t.sendMissing}</p>
              <div className="mt-3"><Button variant="soft" size="md" onClick={() => void act("diagnostics").then((x) => x.ok && toast(t.diagnosticsSaved))}>{t.tryAgain}</Button></div>
            </>
          )}
        </Card>
      </Area>
      {dialog}
    </Screen>
  );
}
