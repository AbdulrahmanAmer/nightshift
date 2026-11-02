/* Line-art spot illustrations on one system (Planify's Spot): 120x100 viewBox, 2 px round strokes in
   currentColor (ink), fills read --ds-illus-paper / --ds-illus-accent so they follow the surface and theme.
   On an accent card wrap them in .ds-illus-on-accent (white paper, navy ink).
   Calendar, Notes, Collaborate and Gift are Planify's drawings of the reference art.
   Story, Clips, Series and Voice are the Nightshift spots drawn in the same style. */
import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

const paper = "var(--ds-illus-paper)";
const accent = "var(--ds-illus-accent)";
const tint = "var(--ds-bg-accent-soft)";
const ink = "currentColor";

function Spot({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <svg className={cx("ds-illus", className)} viewBox="0 0 120 100" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" role="img" aria-label={label}>
      {children}
    </svg>
  );
}

export function CalendarIllustration({ className, label = "Spiral calendar with a highlighted day" }: { className?: string; label?: string }) {
  const cells = [30, 44, 58, 72].flatMap((x) => [44, 56, 68].map((y) => ({ x, y })));
  return (
    <Spot label={label} className={className}>
      <g transform="rotate(-7 60 55)">
        <rect x="22" y="20" width="74" height="68" rx="7" fill={paper} />
        <path d="M22 36h74" />
        {[32, 44, 56, 68, 80].map((x) => <g key={x}><circle cx={x} cy="24" r="2.4" fill={paper} /><path d={`M${x} 12v10`} /></g>)}
        {cells.map(({ x, y }) => <rect key={`${x}-${y}`} x={x} y={y} width="10" height="8" rx="1.6" fill={x === 44 && y === 56 ? accent : x === 30 && y === 68 ? ink : paper} />)}
      </g>
      <path d="M84 66l9 21 3-8.5 8.5-3z" fill={paper} />
    </Spot>
  );
}

export function NotesIllustration({ className, label = "Notes hanging on a line" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <path d="M6 20c34 10 74 10 108-2" />
      <g transform="rotate(-12 30 52)"><rect x="14" y="28" width="34" height="46" rx="3" fill={paper} /><path d="M21 40h20M21 47h14M21 54h18" /><rect x="24" y="22" width="6" height="10" rx="1" fill={ink} /></g>
      <g transform="rotate(9 62 58)"><rect x="48" y="32" width="30" height="42" rx="3" fill={paper} /><rect x="54" y="42" width="18" height="8" rx="1.5" fill={accent} /><path d="M54 58h14M54 64h10" /><rect x="58" y="26" width="6" height="10" rx="1" fill={ink} /></g>
      <g transform="rotate(-5 96 46)"><rect x="82" y="26" width="28" height="36" rx="3" fill={paper} /><path d="M89 37l12 12M101 37L89 49" /><rect x="92" y="20" width="6" height="10" rx="1" fill={ink} /></g>
    </Spot>
  );
}

export function CollaborateIllustration({ className, label = "Shared browser window with message bubble" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <rect x="16" y="20" width="78" height="58" rx="5" fill={paper} /><path d="M16 31h78" />
      <circle cx="23" cy="25.5" r="1.4" fill={ink} /><circle cx="28.5" cy="25.5" r="1.4" fill={ink} />
      <rect x="30" y="40" width="46" height="12" rx="3" fill={accent} /><path d="M37 46h20" stroke={paper} />
      <path d="M28 60h30M28 66h20" />
      <rect x="80" y="8" width="30" height="22" rx="3" fill={paper} /><path d="M86 15h6M100 13l5 5M105 13l-5 5" />
      <rect x="22" y="84" width="16" height="8" rx="2" fill={paper} /><rect x="42" y="84" width="22" height="8" rx="2" fill={ink} />
      <rect x="68" y="84" width="16" height="8" rx="2" fill={paper} /><rect x="88" y="84" width="10" height="8" rx="2" fill={accent} />
    </Spot>
  );
}

export function GiftIllustration({ className, label = "Hands presenting a gift box" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <path d="M26 18v8M22 22h8M96 14v6M93 17h6M100 42l4 4M104 42l-4 4" />
      <circle cx="18" cy="44" r="2" fill={paper} /><circle cx="84" cy="10" r="1.6" fill={ink} />
      <rect x="44" y="44" width="32" height="26" rx="3" fill={ink} />
      <rect x="40" y="36" width="40" height="10" rx="2" fill={paper} />
      <path d="M60 36v34" stroke={paper} />
      <path d="M60 36c-6-10-16-8-14-2 1 3 8 3 14 2zM60 36c6-10 16-8 14-2-1 3-8 3-14 2z" fill={paper} />
      <path d="M50 56l4 3M66 54l3 4" stroke={paper} />
      <path d="M8 98c4-14 12-24 24-30l14 2c4 1 4 7-1 7h-8l-2 4 16 1" fill={paper} />
      <path d="M112 98c-4-14-12-24-24-30l-14 2c-4 1-4 7 1 7h8l2 4-16 1" fill={paper} />
    </Spot>
  );
}

/** Write a story: an open notebook with ruled lines, a pen across it and a small moon. */
export function StoryIllustration({ className, label = "Open notebook with a pen" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <path d="M22 30 L58 24 L58 84 L22 90 Z" fill={tint} stroke="none" />
      <path d="M26 26 L60 20 L60 80 L26 86 Z" fill={paper} />
      <path d="M60 20 L94 26 L94 86 L60 80 Z" fill={paper} />
      <path d="M33 34 L53 30.5 M33 42 L53 38.5 M33 50 L48 47.3 M67 30.5 L87 34 M67 38.5 L87 42 M67 46.5 L80 48.8" />
      <path d="M67 55 L85 58 L82.5 72 L64.6 68.9 Z" fill={accent} />
      <path d="M40 72 L84 38 L90 44 L46 78 L38 80 Z" fill={accent} />
      <path d="M80 41 L86 47" />
      <path d="M100 10 a8 8 0 1 0 8 11 a6 6 0 1 1 -8 -11 Z" fill={accent} stroke="none" />
      <path d="M14 16 v6 M11 19 h6 M104 72 v5 M101.5 74.5 h5" />
    </Spot>
  );
}

/** Cut clips: two film frames pinned to a line, one highlighted, and scissors. */
export function ClipsIllustration({ className, label = "Film frames on a line with scissors" }: { className?: string; label?: string }) {
  const holes = (x: number) => [0, 8, 16, 24, 32, 40].map((d) => `M${x} ${28 + d} v2`).join(" ");
  return (
    <Spot label={label} className={className}>
      <path d="M6 16 Q60 34 114 14" />
      <g transform="rotate(-8 30 52)">
        <rect x="12" y="24" width="38" height="52" rx="3" fill={paper} />
        <rect x="18" y="34" width="26" height="32" rx="2" fill={tint} stroke="none" />
        <path d={`${holes(16)} ${holes(46)}`} />
        <rect x="27" y="16" width="5" height="10" rx="1" fill={ink} />
      </g>
      <g transform="rotate(12 80 54)">
        <rect x="62" y="26" width="38" height="52" rx="3" fill={paper} />
        <rect x="68" y="36" width="26" height="32" rx="2" fill={accent} stroke="none" />
        <path d="M78 45 L88 52 L78 59 Z" fill={paper} stroke="none" />
        <path d={`${holes(66)} ${holes(96)}`} />
        <rect x="78" y="18" width="5" height="10" rx="1" fill={ink} />
      </g>
      <circle cx="44" cy="88" r="5" fill={paper} /><circle cx="58" cy="90" r="5" fill={paper} />
      <path d="M47 84 L64 66 M55 86 L50 64 M104 84 l6 -6 M106 90 l8 -2" />
    </Spot>
  );
}

/** Build a series: three stacked episode cards, a play mark on the front one and numbered chips. */
export function SeriesIllustration({ className, label = "Stacked episode cards" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <rect x="34" y="12" width="62" height="42" rx="5" fill={tint} />
      <rect x="26" y="24" width="62" height="42" rx="5" fill={paper} />
      <rect x="18" y="36" width="62" height="46" rx="5" fill={paper} />
      <path d="M23 36 h52 a5 5 0 0 1 5 5 v7 h-62 v-7 a5 5 0 0 1 5 -5 Z" fill={accent} />
      <circle cx="25" cy="42" r="2" fill={paper} /><circle cx="32" cy="42" r="2" fill={paper} />
      <path d="M44 56 L58 64 L44 72 Z" fill={accent} />
      <rect x="86" y="70" width="12" height="9" rx="2" fill={accent} /><rect x="100" y="70" width="12" height="9" rx="2" fill={paper} />
      <rect x="86" y="82" width="26" height="6" rx="2" fill={paper} />
      <path d="M100 18 l6 -6 M104 22 h7 M10 26 v6 M7 29 h6" />
    </Spot>
  );
}

/** Train a voice: two open hands holding up a microphone with sound waves. Drawn for the accent card. */
export function VoiceIllustration({ className, label = "Hands holding up a microphone" }: { className?: string; label?: string }) {
  return (
    <Spot label={label} className={className}>
      <path d="M30 14 q4 -6 8 0 M88 10 l4 4 m0 -4 l-4 4 M18 40 q-6 -4 -2 -10 M100 36 q8 -2 6 -10" />
      <circle cx="22" cy="20" r="3" fill={paper} /><circle cx="98" cy="48" r="2.5" fill={ink} stroke="none" />
      <path d="M40 34 q-6 8 0 16 M80 34 q6 8 0 16 M33 29 q-10 13 0 26 M87 29 q10 13 0 26" />
      <rect x="50" y="18" width="20" height="34" rx="10" fill={ink} />
      <path d="M54 28 h12 M54 34 h12 M54 40 h12" stroke={paper} />
      <path d="M60 52 v10" />
      <path d="M26 92 L26 70 Q26 62 34 64 L46 68 Q54 70 56 64 L60 62 L64 64 Q66 70 74 68 L86 64 Q94 62 94 70 L94 92" fill={paper} />
      <path d="M36 74 q4 6 12 6 M84 74 q-4 6 -12 6" />
    </Spot>
  );
}
