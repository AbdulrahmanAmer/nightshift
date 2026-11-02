// 2.10 Uninstall, and its result screen (the Done layout with a "Kept" card).
import { useState } from "react";
import { Button, Card, CardHeader } from "@ref12";
import { uninstall as t } from "../copy";
import { gb } from "../format";
import { useSetup } from "../api/useSetup";
import type { UninstallView } from "../api/types";
import { Area, Screen } from "../parts/Screen";
import { ChoiceRow } from "../parts/ChoiceRow";
import { ConfirmDialog } from "../parts/ConfirmDialog";

export function Uninstall({ view }: { view: UninstallView }) {
  const { api, removeNow, busy, toast } = useSetup();
  const [models, setModels] = useState(false);
  const [data, setData] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const running = view.running || busy.has("uninstall");
  const close = async () => { try { await api?.action("close-setup"); } catch { /* the window closes anyway */ } };
  const reveal = (path: string) => void api?.action("reveal", { path }).catch((e) => toast(e instanceof Error ? e.message : "Setup did not answer"));

  if (view.result) {
    const r = view.result;
    return (
      <Screen name="uninstall-done" salutation={t.doneSalutation} description={t.doneDescription}
        footerLeft={view.home ?? ""} footerRight={<Button variant="primary" size="lg" data-primary onClick={() => void close()}>{t.close}</Button>}>
        <Area area="kept">
          <Card as="section" padding="lg" aria-label={t.keptTitle}>
            <CardHeader title={t.keptTitle} level={2} />
            {r.kept.length === 0 ? <p className="text-ds-sm text-ds-text-2">{t.keptNone}</p> : (
              <ul className="flex flex-col gap-3">
                {r.kept.map((k) => (
                  <li key={k.path} className="flex items-center justify-between gap-4">
                    <div className="min-w-0"><p className="text-ds-sm font-medium">{k.label}</p><p className="truncate font-mono text-ds-xs text-ds-text-2" title={k.path}>{k.path}</p></div>
                    <Button variant="soft" size="md" leadingIcon="folder" onClick={() => reveal(k.path)}>{t.reveal}</Button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </Area>
      </Screen>
    );
  }

  const always = view.always.length ? view.always : t.alwaysFallback;
  const willGo = [...always.map((a) => a.label.toLowerCase()), ...(models ? ["the AI models"] : []), ...(data ? ["your videos and history"] : [])];
  const home = view.home ?? "";
  const BS = "\\";
  const kept = !models && !data ? `${home}${BS}models, outbox, data` : !models ? `${home}${BS}models` : !data ? `${home}${BS}outbox, data` : "";
  return (
    <>
      <Screen name="uninstall" salutation={t.salutation} question={t.question}
        footerLeft={running ? t.footerRunning : kept ? t.footerKept(kept) : ""}
        footerRight={
          <>
            <Button variant="soft" size="lg" disabled={running} onClick={() => void close()}>{t.cancel}</Button>
            <Button variant="primary" size="lg" data-primary disabled={running} onClick={() => setConfirm(true)}>{running ? t.removing : t.remove}</Button>
          </>
        }>
        <Area area="gone">
          <Card as="section" padding="lg" aria-label={t.goneTitle}>
            <CardHeader title={t.goneTitle} level={2} />
            <ul className="flex flex-col gap-3">
              {always.map((a) => (
                <li key={a.label} className="flex items-baseline justify-between gap-4 text-ds-sm"><span>{a.label}</span>{a.bytes != null && <span className="ds-num text-ds-text-2">{gb(a.bytes)}</span>}</li>
              ))}
            </ul>
          </Card>
        </Area>
        <Area area="keep">
          <Card as="section" padding="lg" aria-label={t.keepTitle}>
            <CardHeader title={t.keepTitle} level={2} />
            <div className="-mx-3 flex flex-col gap-1">
              <ChoiceRow type="checkbox" checked={models} onChange={setModels} disabled={running} label={t.deleteModels} hint={t.deleteModelsHint} trailing={gb(view.models.bytes)} />
              <ChoiceRow type="checkbox" checked={data} onChange={setData} disabled={running} label={t.deleteData} hint={t.deleteDataHint} trailing={gb(view.data.bytes)} />
            </div>
          </Card>
        </Area>
      </Screen>
      {confirm && (
        <ConfirmDialog title={t.confirmTitle} body={t.confirmBody(willGo)} onCancel={() => setConfirm(false)}
          actions={[
            { label: t.confirmKeep, variant: "soft", onClick: () => setConfirm(false), initial: true },
            { label: t.remove, variant: "primary", onClick: () => { setConfirm(false); void removeNow({ deleteModels: models, deleteData: data }); } },
          ]} />
      )}
    </>
  );
}
