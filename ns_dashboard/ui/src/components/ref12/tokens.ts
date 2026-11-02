/* Shared types and class maps for the ref12 component sheets. Values live in styles.css (--r12-*). */
export type Tone = "accent" | "green" | "red" | "muted" | "ink";

/** Solid dot / stroke colour per tone. */
export const TONE_BG: Record<Tone, string> = { accent: "bg-r12-accent", green: "bg-r12-green", red: "bg-r12-red", muted: "bg-r12-muted", ink: "bg-r12-ink" };
/** Readable text colour per tone. */
export const TONE_TEXT: Record<Tone, string> = { accent: "text-r12-accent-ink", green: "text-r12-green-ink", red: "text-r12-red-ink", muted: "text-r12-muted", ink: "text-r12-ink" };
/** Raw colour for SVG strokes. */
export const TONE_VAR: Record<Tone, string> = { accent: "var(--r12-accent)", green: "var(--r12-green)", red: "var(--r12-red)", muted: "var(--r12-muted)", ink: "var(--r12-ink)" };

/** Lucide defaults used everywhere in the sheet: one stroke, three sizes. */
export const ICON = { sm: 14, md: 16, lg: 20, stroke: 1.75 } as const;
