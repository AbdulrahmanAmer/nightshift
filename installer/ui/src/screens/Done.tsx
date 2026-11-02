// 2.7 Done.
import { useEffect, useState } from "react";
import { Button, Card, CardHeader, DottedRule, StatusDot } from "@ref12";
import { done as t } from "../copy";
import { duration } from "../format";
import { useLiveState } from "../api/useSetup";
import { Area, Screen } from "../parts/Screen";
import { useCopyReport } from "../parts/useCopyReport";

export function Done() {
  const { state, act, toast } = useLiveState();
  const { copy, dialog } = useCopyReport();
  const [opening, setOpening] = useState(false);
  useEffect(() => { if (!opening) return; const id = window.setTimeout(() => setOpening(false), 3000); return () => window.clearTimeout(id); }, [opening]);
  const r = state.result;
  if (!r) return null;
  const zip = r.diagnosticsZip;
  const zipName = zip ? zip.split(/[\\/]/).pop() : null;
  const open = async () => { setOpening(true); await act("open-dashboard"); };
  const warnings = [...r.warnings.map((w) => w.title), ...(state.machine.isAdmin ? [t.adminNote] : [])];
  return (
    <Screen name="done" salutation={t.salutation} description={r.scheduled ? t.description(r.runTime) : t.descriptionNoTask}
      footerLeft={opening ? t.opening : t.footer(duration(r.durationS))}
      footerRight={
        <>
          <Button variant="soft" size="lg" leadingIcon="copy" onClick={() => void copy()}>{t.copyReport}</Button>
          <Button variant="primary" size="lg" data-primary disabled={opening} onClick={() => void open()}>{t.openDashboard}</Button>
        </>
      }>
      <Area area="inst">
        <Card as="section" padding="lg" aria-label={t.installedTitle}>
          <CardHeader title={t.installedTitle} level={2} />
          <dl className="flex flex-col gap-2.5">
            {r.installed.map((i) => (
              <div key={i.label} className="flex items-baseline justify-between gap-4 text-ds-sm">
                <dt className="min-w-0">{i.label}</dt>
                <dd className="max-w-[55%] shrink-0 truncate text-right ds-num" title={i.detail}>{i.detail}</dd>
              </div>
            ))}
          </dl>
          {warnings.length > 0 && (
            <ul className="mt-4 flex flex-col gap-1.5">
              {warnings.map((w) => <li key={w} className="flex items-center gap-2 text-ds-xs text-ds-text-2"><StatusDot tone="attention" size={5} />{w}</li>)}
            </ul>
          )}
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
              <p className="mt-2 text-ds-sm leading-ds-normal text-ds-text-2">{t.sendBody}</p>
              <div className="mt-3"><Button variant="soft" size="md" leadingIcon="folder" onClick={() => void act("reveal", { path: zip })}>{t.showFile}</Button></div>
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
