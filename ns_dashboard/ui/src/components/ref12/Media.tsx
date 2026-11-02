import { cx } from "../../ui/kit";
import { Icon } from "./Icon";

/** Deterministic bar heights (0.25-1) for a waveform when no audio analysis is available, seeded by a string. */
export function sampleWaveform(count = 32, seed = "voice"): number[] {
  let s = 0; for (const ch of seed) s = (s * 31 + ch.charCodeAt(0)) % 997;
  return Array.from({ length: count }, (_, i) => 0.25 + Math.abs(Math.sin(i * 0.9 + s) * 0.6 + Math.sin(i * 2.3 + s * 2) * 0.4) * 0.75);
}

/** Waveform (Planify Media.tsx): 3 px pill bars, 2 px apart, in a 36 px row. Bars before `progress` (0-1) take the
 *  accent, the rest the muted ink at 60% (one accent per screen). Decorative. */
export function Waveform({ bars, progress, className }: { bars: number[]; progress: number; className?: string }) {
  const played = Math.round(bars.length * Math.max(0, Math.min(1, progress)));
  return (
    <span aria-hidden className={cx("flex h-9 items-center gap-0.5", className)}>
      {bars.map((h, i) => <span key={i} className={cx("max-w-[3px] min-w-0.5 flex-1 rounded-full transition-colors duration-200", i < played ? "bg-ds-accent" : "bg-ds-muted opacity-60")} style={{ height: `${Math.round(h * 100)}%` }} />)}
    </span>
  );
}

/** TransportControls (Planify): previous, play/pause knob, next on a sunken 56 px plate. The knob is pressed in
 *  while paused and raised while playing. Previous and next render only when their handlers are given. */
export function TransportControls({ playing, onToggle, onPrev, onNext, label, playLabel = "Play", pauseLabel = "Pause", prevLabel = "Previous", nextLabel = "Next", disabled, className }: {
  playing: boolean; onToggle: () => void; onPrev?: () => void; onNext?: () => void; label: string;
  playLabel?: string; pauseLabel?: string; prevLabel?: string; nextLabel?: string; disabled?: boolean; className?: string;
}) {
  const skip = "ds-focus grid size-8 place-items-center rounded-ds-sm text-ds-accent-ink transition-colors duration-[120ms] hover:bg-ds-hover";
  return (
    <div role="group" aria-label={label} className={cx("flex h-14 items-center justify-between gap-3 rounded-ds-lg bg-ds-sunken px-4 shadow-[var(--ds-shadow-1),0_0_0_1px_var(--ds-border-subtle)]", className)}>
      {onPrev ? <button type="button" aria-label={prevLabel} title={prevLabel} onClick={onPrev} className={skip}><Icon name="skipBack" size={16} filled /></button> : <span className="size-8" />}
      <button type="button" aria-label={playing ? pauseLabel : playLabel} aria-pressed={playing} onClick={onToggle} disabled={disabled}
        className={cx("ds-focus grid size-10 place-items-center rounded-full bg-ds-surface transition-[box-shadow,color] duration-200 ease-ds-out disabled:cursor-not-allowed disabled:opacity-50",
          playing ? "text-ds-accent-ink shadow-[var(--ds-shadow-control),0_0_0_1px_var(--ds-border-subtle)]" : "text-ds-text-2 shadow-[var(--ds-shadow-inset),0_0_0_1px_var(--ds-border-default)]")}>
        <Icon name={playing ? "pause" : "play"} size={16} filled />
      </button>
      {onNext ? <button type="button" aria-label={nextLabel} title={nextLabel} onClick={onNext} className={skip}><Icon name="skipForward" size={16} filled /></button> : <span className="size-8" />}
    </div>
  );
}
