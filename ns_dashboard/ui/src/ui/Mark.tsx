/* The Nightshift mark: a crescent of lamplight on ink. Flat, one shape. */
export function Mark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="10" fill="var(--fill)" />
      <circle cx="16" cy="16" r="8" fill="var(--accent)" />
      <circle cx="20" cy="13" r="7" fill="var(--fill)" />
    </svg>
  );
}
