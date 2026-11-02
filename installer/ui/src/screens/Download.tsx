// 2.4 Download: one row per file, honest speed and time left, pause and resume.
import { useEffect, useRef } from "react";
import { Button, Card, CardHeader } from "@ref12";
import { download as t } from "../copy";
import { aboutDuration, gbNum, mbps, minutesLeft, pct } from "../format";
import { useLiveState } from "../api/useSetup";
import type { DownloadRow, PlanGroup } from "../api/types";
import { Area, Screen } from "../parts/Screen";
import { DownloadRowItem } from "../parts/DownloadRowItem";
import { TimeLeftCard } from "../parts/Widgets";

const GROUPS: PlanGroup["id"][] = ["tools", "python", "programs", "models"];
const LIVE = new Set<DownloadRow["status"]>(["active", "copying", "verifying", "extracting", "paused"]);

export function Download({ onStop }: { onStop: () => void }) {
  const { state, act, busy, announce } = useLiveState();
  const dl = state.downloads;
  const o = dl.overall;
  const paused = state.run === "paused" || dl.paused;
  const pausing = state.run === "pausing";
  const stopping = state.run === "stopping";
  const toolRunning = dl.items.some((r) => (r.via === "winget" || r.via === "uv") && r.status === "active");
  const total = gbNum(o.bytesTotal);

  // Progress is announced to screen readers at most every 30 seconds.
  const latest = useRef({ percent: 0, etaS: o.etaS });
  latest.current = { percent: pct(o.bytesDone, o.bytesTotal), etaS: o.etaS };
  useEffect(() => {
    const id = window.setInterval(() => { const { percent, etaS } = latest.current; if (etaS) announce(t.announce(percent, minutesLeft(etaS))); }, 30000);
    return () => window.clearInterval(id);
  }, [announce]);

  const toggle = () => void act(paused ? "resume" : "pause");
  const toggleLabel = paused ? t.resume : pausing ? t.pausing : toolRunning ? t.pauseAfter : t.pause;
  const disabled = pausing || stopping || busy.has("pause") || busy.has("resume");
  const srcCard = dl.source === "folder"
    ? { title: t.source.folder.title, body: t.source.folder.body(state.answers?.modelsSource.path ?? "your folder") }
    : dl.source === "drive-zip"
      ? { title: t.source.driveZip.title, body: t.source.driveZip.body }
      : { title: t.source.internet.title, body: t.source.internet.body };

  return (
    <Screen name="downloading" salutation={t.hero(total, o.etaS ? aboutDuration(o.etaS) : null, paused)} description={t.description}
      headerRight={<Button variant="soft" size="md" leadingIcon={paused ? "play" : "pause"} disabled={disabled} onClick={toggle}>{toggleLabel}</Button>}
      footerLeft={stopping ? t.stopping : t.footer(o.filesDone, o.filesTotal, gbNum(o.bytesDone), total)}
      footerRight={
        <>
          <Button variant="ghost" size="lg" disabled={stopping} onClick={onStop}>{t.stop}</Button>
          <Button variant="primary" size="lg" data-primary disabled={disabled} onClick={toggle}>{toggleLabel}</Button>
        </>
      }>
      <Area area="list" className="ns-list">
        <Card as="section" padding="md" aria-label={t.salutation(total)} className="scroll-thin overflow-y-auto">
          {GROUPS.map((g) => {
            const rows = dl.items.filter((r) => r.group === g);
            if (rows.length === 0) return null;
            const sorted = [...rows.filter((r) => LIVE.has(r.status)), ...rows.filter((r) => !LIVE.has(r.status))];
            return (
              <section key={g} aria-label={t.groups[g]} className="mb-2 last:mb-0">
                <CardHeader title={t.groups[g]} level={3} className="mb-1 px-3 pt-2" />
                <ul className="flex flex-col gap-1">{sorted.map((r) => <DownloadRowItem key={r.key} row={r} onRetry={() => void act("retry")} />)}</ul>
              </section>
            );
          })}
        </Card>
      </Area>
      <Area area="eta">
        <TimeLeftCard title={t.etaTitle} etaS={paused ? null : o.etaS} emptyText={paused ? t.paused : t.measuring}>
          <span>{t.of(gbNum(o.bytesDone), total)}</span>
          {o.speedBps && !paused ? <span className="text-ds-text-2">{mbps(o.speedBps)}</span> : null}
        </TimeLeftCard>
      </Area>
      <Area area="src" fit>
        <Card elevation={0} tone="sunken" padding="md" radius="lg" className="flex flex-col gap-1">
          <h2 className="text-ds-md leading-ds-snug font-semibold">{srcCard.title}</h2>
          <p className="text-ds-xs leading-ds-normal text-ds-text-2">{srcCard.body}</p>
        </Card>
      </Area>
    </Screen>
  );
}
