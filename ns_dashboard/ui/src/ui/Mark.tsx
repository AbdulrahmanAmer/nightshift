/* The Nightshift mark: a crescent of lamplight on ink. Flat, one shape.
   tone="white" is the bare crescent for the dark purple side tab. */
export function Mark({ size = 28, tone = "ink" }: { size?: number; tone?: "ink" | "white" }) {
  if (tone === "white") {
    return (
      <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
        <mask id="ns-mark-cut"><rect width="32" height="32" fill="#fff" /><circle cx="20.5" cy="12.5" r="7.5" fill="#000" /></mask>
        <circle cx="15" cy="17" r="10" fill="#fff" mask="url(#ns-mark-cut)" />
      </svg>
    );
  }
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="10" fill="var(--fill)" />
      <circle cx="16" cy="16" r="8" fill="var(--accent)" />
      <circle cx="20" cy="13" r="7" fill="var(--fill)" />
    </svg>
  );
}
