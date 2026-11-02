/* Line-art spot illustrations in the reference's style: 2 px round ink strokes, flat accent fills,
   a pale accent tint for secondary planes, white paper. Classes are defined in styles.css (.r12-ill).
   Each is a 120x100 viewBox and scales with its container. Decorative: aria-hidden. */

/** Write a story: an open notebook with ruled lines, a pen across it and a small moon. */
export function StoryIllustration() {
  return (
    <svg viewBox="0 0 120 100" className="r12-ill" aria-hidden>
      <path className="t" d="M22 30 L58 24 L58 84 L22 90 Z" />
      <path className="w s" d="M26 26 L60 20 L60 80 L26 86 Z" />
      <path className="w s" d="M60 20 L94 26 L94 86 L60 80 Z" />
      <path className="s" d="M33 34 L53 30.5 M33 42 L53 38.5 M33 50 L48 47.3 M67 30.5 L87 34 M67 38.5 L87 42 M67 46.5 L80 48.8" />
      <rect className="a" x="67" y="55" width="18" height="14" rx="2" transform="rotate(10 76 62)" />
      <path className="s" d="M67 55 L85 58 L82.5 72 L64.6 68.9 Z" />
      <path className="a s" d="M40 72 L84 38 L90 44 L46 78 L38 80 Z" />
      <path className="s" d="M80 41 L86 47" />
      <path className="a" d="M100 10 a8 8 0 1 0 8 11 a6 6 0 1 1 -8 -11 Z" />
      <path className="s" d="M14 16 v6 M11 19 h6 M104 72 v5 M101.5 74.5 h5" />
    </svg>
  );
}

/** Cut clips: a film strip pinned to a line, one frame lifted out, and scissors. */
export function ClipsIllustration() {
  return (
    <svg viewBox="0 0 120 100" className="r12-ill" aria-hidden>
      <path className="s" d="M6 16 Q60 34 114 14" />
      <rect className="k" x="27" y="16" width="4" height="10" rx="1" />
      <rect className="k" x="79" y="18" width="4" height="10" rx="1" />
      <g transform="rotate(-8 30 52)">
        <rect className="w s" x="12" y="24" width="38" height="52" rx="3" />
        <rect className="t" x="18" y="34" width="26" height="32" rx="2" />
        <path className="s" d="M16 28 v2 M16 36 v2 M16 44 v2 M16 52 v2 M16 60 v2 M16 68 v2 M46 28 v2 M46 36 v2 M46 44 v2 M46 52 v2 M46 60 v2 M46 68 v2" />
      </g>
      <g transform="rotate(12 80 54)">
        <rect className="w s" x="62" y="26" width="38" height="52" rx="3" />
        <rect className="a" x="68" y="36" width="26" height="32" rx="2" />
        <path className="w" d="M78 45 L88 52 L78 59 Z" />
        <path className="s" d="M66 30 v2 M66 38 v2 M66 46 v2 M66 54 v2 M66 62 v2 M66 70 v2 M96 30 v2 M96 38 v2 M96 46 v2 M96 54 v2 M96 62 v2 M96 70 v2" />
      </g>
      <circle className="w s" cx="44" cy="88" r="5" />
      <circle className="w s" cx="58" cy="90" r="5" />
      <path className="s" d="M47 84 L64 66 M55 86 L50 64" />
      <path className="s" d="M104 84 l6 -6 M106 90 l8 -2" />
    </svg>
  );
}

/** Build a series: three stacked episode cards with numbered chips and a play mark on the front one. */
export function SeriesIllustration() {
  return (
    <svg viewBox="0 0 120 100" className="r12-ill" aria-hidden>
      <rect className="t s" x="34" y="12" width="62" height="42" rx="5" />
      <rect className="w s" x="26" y="24" width="62" height="42" rx="5" />
      <rect className="w s" x="18" y="36" width="62" height="46" rx="5" />
      <rect className="a" x="18" y="36" width="62" height="12" rx="5" />
      <path className="s" d="M18 48 h62" />
      <circle className="w s" cx="25" cy="42" r="2" />
      <circle className="w s" cx="32" cy="42" r="2" />
      <path className="a s" d="M44 56 L58 64 L44 72 Z" />
      <rect className="a" x="86" y="70" width="12" height="9" rx="2" />
      <rect className="s" x="86" y="70" width="12" height="9" rx="2" />
      <rect className="w s" x="100" y="70" width="12" height="9" rx="2" />
      <rect className="w s" x="86" y="82" width="26" height="6" rx="2" />
      <path className="s" d="M100 18 l6 -6 M104 22 h7 M10 26 v6 M7 29 h6" />
    </svg>
  );
}

/** Train a voice: two open hands holding up a microphone with sound waves. Drawn for the accent card. */
export function VoiceIllustration() {
  return (
    <svg viewBox="0 0 120 100" className="r12-ill r12-ill-inv" aria-hidden>
      <path className="s" d="M30 14 q4 -6 8 0 M88 10 l4 4 m0 -4 l-4 4 M18 40 q-6 -4 -2 -10 M100 36 q8 -2 6 -10" />
      <circle className="a" cx="22" cy="20" r="3" />
      <circle className="a" cx="98" cy="48" r="2.5" />
      <path className="s" d="M40 34 q-6 8 0 16 M80 34 q6 8 0 16 M33 29 q-10 13 0 26 M87 29 q10 13 0 26" />
      <rect className="a s" x="50" y="18" width="20" height="34" rx="10" />
      <path className="sw" d="M54 28 h12 M54 34 h12 M54 40 h12" />
      <path className="s" d="M60 52 v10" />
      <path className="w s" d="M26 92 L26 70 Q26 62 34 64 L46 68 Q54 70 56 64 L60 62 L64 64 Q66 70 74 68 L86 64 Q94 62 94 70 L94 92" />
      <path className="s" d="M36 74 q4 6 12 6 M84 74 q-4 6 -12 6" />
    </svg>
  );
}
