// 2.6 Verify: real output the partner can see and hear. The installer's one rich moment (test frame and narrator waveform).
import { useEffect, useRef, useState } from "react";
import { Button, Card, CardHeader, IconButton, Waveform } from "@ref12";
import { codeText, verify as t } from "../copy";
import { clock } from "../format";
import { useLiveState } from "../api/useSetup";
import type { Notice, VerifyRow } from "../api/types";
import { Area, Screen } from "../parts/Screen";
import { VerifyRowItem } from "../parts/RowItems";
import { IssueCard } from "../parts/IssueCard";
import { SetupIcon } from "../parts/icons";
import { useCopyReport } from "../parts/useCopyReport";
import { CopyStatusButton } from "../parts/CopyStatus";

function labelFor(name: string, label: string): string {
  if (label) return label;
  const hit = t.labelByPrefix.find(([p]) => name.startsWith(p));
  return hit ? hit[1] : name;
}

/** The narrator sample: IconButton, Waveform, length. Audio plays through new Audio(url). */
function VoiceCard() {
  const { state } = useLiveState();
  const v = state.verify?.voice ?? null;
  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  useEffect(() => () => { audio.current?.pause(); audio.current = null; }, []);
  if (!v) return null;
  const toggle = () => {
    if (!audio.current) {
      const a = new Audio(v.url);
      a.addEventListener("timeupdate", () => setTime(a.currentTime));
      a.addEventListener("ended", () => { setPlaying(false); setTime(0); });
      a.addEventListener("pause", () => setPlaying(false));
      a.addEventListener("play", () => setPlaying(true));
      a.addEventListener("error", () => setPlaying(false));
      audio.current = a;
    }
    if (audio.current.paused) void audio.current.play().catch(() => setPlaying(false)); else audio.current.pause();
  };
  return (
    <Card as="section" padding="md" aria-label={v.speaker}>
      <CardHeader title={v.speaker} level={2} className="mb-3" />
      <div className="flex items-center gap-3">
        <IconButton icon={playing ? "pause" : "play"} label={playing ? t.pauseSample : t.playSample} variant="accent" round size="md" onClick={toggle} />
        <Waveform bars={v.bars} progress={v.seconds ? time / v.seconds : 0} className="min-w-0 flex-1" />
        <span className="shrink-0 text-ds-xs text-ds-text-2 ds-num">{clock(v.seconds)}</span>
      </div>
      <p className="mt-3 text-ds-sm leading-ds-normal text-ds-text-2">{v.text}</p>
    </Card>
  );
}

function RenderCard() {
  const { state } = useLiveState();
  const r = state.verify?.render ?? null;
  const note = state.verify?.renderNote ?? null;
  const [playing, setPlaying] = useState(false);
  return (
    <Card as="section" padding="md" aria-label={t.renderTitle} className="flex flex-col items-center gap-3">
      <div className="relative grid h-[320px] w-[180px] place-items-center overflow-hidden rounded-ds-lg bg-ds-sunken">
        {r && playing && r.videoUrl ? <video src={r.videoUrl} controls autoPlay className="size-full object-cover" />
          : r ? <img src={r.thumbUrl} alt={t.frameAlt} width={180} height={320} className="size-full object-cover" />
          : (
            <div className="flex flex-col items-center gap-3 px-4 text-center text-ds-xs text-ds-muted">
              <SetupIcon name="film" size={22} />
              <span>{note ?? t.renderPending}</span>
            </div>
          )}
      </div>
      {r && (
        <div className="flex w-[180px] flex-col gap-2">
          <p className="text-ds-xs text-ds-text-2 ds-num">{t.caption(clock(r.seconds), t.encoder[r.encoder])}</p>
          {r.videoUrl && !playing && <div><Button variant="soft" size="sm" leadingIcon="play" onClick={() => setPlaying(true)}>{t.play}</Button></div>}
        </div>
      )}
    </Card>
  );
}

export function Verify() {
  const { state } = useLiveState();
  const { copy, dialog } = useCopyReport();
  const v = state.verify;
  const step = (id: string) => state.steps.find((s) => s.id === id);
  const stepStatus = (id: string): VerifyRow["status"] => {
    const s = step(id)?.status;
    return s === "done" ? "passed" : s === "running" ? "running" : s === "failed" ? "failed" : s === "warning" ? "fallback" : "pending";
  };
  const rows = v?.checks ?? [];
  const extra = [
    { id: "s09_shortcuts_scheduler", label: t.extraRows.s09_shortcuts_scheduler },
    { id: "s10_summary", label: t.extraRows.s10_summary },
  ];
  const total = rows.length + extra.length;
  const doneN = rows.filter((r) => r.status === "passed" || r.status === "fallback" || r.status === "failed").length + extra.filter((e) => stepStatus(e.id) === "passed").length;
  const notices: Notice[] = state.notices;
  const sampleStatus = step("v01_samples")?.status;
  const sampleDone = sampleStatus === "done" || sampleStatus === "warning" || sampleStatus === "failed" || sampleStatus === "skipped";
  return (
    <Screen name="verifying" salutation={t.salutation} description={t.description}
      footerLeft={t.footer(doneN, total)}
      footerRight={<><CopyStatusButton /><Button variant="soft" size="lg" leadingIcon="copy" data-primary disabled={!v?.reportReady} onClick={() => void copy()}>{t.copyReport}</Button></>}>
      <Area area="checks">
        <Card as="section" padding="md" aria-label={t.checksTitle}>
          <CardHeader title={t.checksTitle} level={2} className="mb-3 px-3" />
          <ul className="flex flex-col gap-1">
            {rows.map((r) => <VerifyRowItem key={r.name} label={labelFor(r.name, r.label)} status={r.status} value={r.value} />)}
            {extra.map((e) => <VerifyRowItem key={e.id} label={e.label} status={stepStatus(e.id)} value={null} />)}
          </ul>
        </Card>
      </Area>
      <Area area="render"><RenderCard /></Area>
      <Area area="voice" className="ns-voice"><VoiceCard /></Area>
      <Area area="notes" className="ns-notes scroll-thin overflow-y-auto">
        <div className="flex flex-col gap-3">
          {sampleDone && state.verify?.voice == null && <p className="px-1 text-ds-sm text-ds-text-2">{t.noVoice}</p>}
          {notices.map((n) => {
            const c = codeText(n.code);
            return <IssueCard key={n.code + n.title} tone="info" code={n.code || null} title={n.title || c.title} whatToDo={n.whatToDo || c.whatToDo} />;
          })}
        </div>
      </Area>
      {dialog}
    </Screen>
  );
}
