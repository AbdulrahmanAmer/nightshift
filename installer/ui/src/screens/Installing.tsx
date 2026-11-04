// 2.5 Installing: the steps after the downloads, as a short list with a count. No invented percentages.
import { Button, Card } from "@ref12";
import { installing as t } from "../copy";
import { hhmm } from "../format";
import { useLiveState } from "../api/useSetup";
import { Area, Screen } from "../parts/Screen";
import { StepItem } from "../parts/RowItems";
import { DriveStepper } from "../parts/Widgets";
import { CopyStatusButton } from "../parts/CopyStatus";

export function Installing({ onStop }: { onStop: () => void }) {
  const { state } = useLiveState();
  const steps = state.steps.filter((s) => s.phase === "installing");
  const count = steps.length;
  const doneCount = steps.filter((s) => s.status === "done" || s.status === "skipped").length;
  const latest = state.log[state.log.length - 1] ?? null;
  const stopping = state.run === "stopping";
  return (
    <Screen name="installing" salutation={t.salutation} description={t.description}
      footerLeft={t.footer(Math.min(count, doneCount + 1), count)}
      footerRight={<><CopyStatusButton /><Button variant="ghost" size="lg" disabled={stopping} onClick={onStop}>{t.stop}</Button></>}>
      <Area area="stepper"><DriveStepper title={t.stepsTitle} value={doneCount} max={count} unit={t.unit} /></Area>
      <Area area="list" className="ns-list-fit">
        <Card as="section" padding="md" aria-label={t.stepsTitle}>
          <ul className="flex h-full flex-col justify-around">{steps.map((s) => <StepItem key={s.id} step={s} />)}</ul>
        </Card>
      </Area>
      <Area area="log">
        <Card elevation={0} tone="sunken" padding="sm" radius="lg" className="flex items-center gap-3 text-ds-xs">
          <span className="shrink-0 font-medium text-ds-text-2">{t.latest}</span>
          <span className="min-w-0 truncate font-mono text-ds-text-2" aria-hidden={!latest}>{latest ? `${hhmm(latest.t)}  ${latest.text}` : t.noLog}</span>
        </Card>
      </Area>
    </Screen>
  );
}
