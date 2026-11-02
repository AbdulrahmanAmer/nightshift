// The story model picker of the Choices screen (docs/design/installer-ui.md section 2.3): one card per tier with the model
// from manifest.json, its download size, the video memory it leaves for context on a 16 GB card, what it is good for and a
// "Recommended" badge on Balanced. An optional extra card (Other: a Hugging Face file the owner names) sits in the same group.
import type { ReactNode } from "react";
import { Badge } from "@ref12";
import { cx } from "@dash/ui/kit";
import { choices as t } from "../copy";
import { gbNum } from "../format";
import type { Tier, TierInfo } from "../api/types";

const TIERS: Tier[] = ["small", "balanced", "best"];

/** "Qwen3-14B-Q4_K_M.gguf" -> "Qwen3-14B-Q4_K_M". */
export const modelName = (file: string | null | undefined): string => (file ?? "").replace(/^.*\//, "").replace(/\.gguf$/i, "");

/** One line about the video memory left: "6.4 GB left for context", or the plain words when the model fills the card. */
export function vramLine(left: number | null): string | null {
  if (left == null) return null;
  return left >= 0.5 ? t.picker.vramLeft(left.toFixed(1)) : t.picker.vramNone;
}

export const cardClass = (checked: boolean, disabled?: boolean) =>
  cx(
    "flex min-w-0 cursor-pointer flex-col gap-3 rounded-ds-lg border p-4 transition-colors duration-[120ms] ease-ds-out has-[:focus-visible]:shadow-ds-focus",
    checked ? "border-ds-accent bg-ds-sunken shadow-ds-selected" : "border-ds-border-subtle hover:bg-ds-hover",
    disabled && "cursor-not-allowed opacity-60",
  );

function TierCard({ tier, info, checked, disabled, onPick }: { tier: Tier; info: TierInfo; checked: boolean; disabled: boolean; onPick: () => void }) {
  const vram = vramLine(info.vramLeftGb);
  return (
    <label className={cardClass(checked, disabled)}>
      <input type="radio" name="llm-model" className="sr-only" checked={checked} disabled={disabled} onChange={onPick} />
      <span className="flex items-center justify-between gap-2">
        <span className="text-ds-md leading-ds-snug font-semibold">{t.picker.names[tier]}</span>
        {info.recommended && info.allowed && <Badge tone="accent" size="sm">{t.picker.recommended}</Badge>}
      </span>
      <span className="min-w-0">
        <span className="block truncate font-mono text-ds-xs" title={info.file ?? undefined}>{modelName(info.file) || t.picker.unknownModel}</span>
        <span className="block truncate text-ds-2xs text-ds-muted" title={info.repo ?? undefined}>{info.repo ?? ""}</span>
      </span>
      <span className="flex flex-col gap-0.5 text-ds-xs ds-num">
        <span className="font-medium">{t.picker.download(gbNum(info.llmBytes))}</span>
        {vram && <span className="text-ds-text-2">{vram}</span>}
      </span>
      <span className="text-ds-xs leading-ds-normal text-ds-text-2">{t.picker.goodFor[tier]}</span>
      {!info.allowed && <Badge tone="danger" size="sm" className="self-start">{info.reason ?? t.picker.notAllowed}</Badge>}
    </label>
  );
}

export function ModelPicker({ tiers, value, onChange, disabled, other, children }: {
  tiers: Record<Tier, TierInfo>; value: Tier | "other"; onChange: (tier: Tier) => void; disabled?: boolean; other?: ReactNode; children?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div role="radiogroup" aria-label={t.picker.label} className="grid grid-cols-2 gap-3 @min-[1020px]:grid-cols-4">
        {TIERS.map((tier) => <TierCard key={tier} tier={tier} info={tiers[tier]} checked={value === tier} disabled={!!disabled} onPick={() => onChange(tier)} />)}
        {other}
      </div>
      {children}
    </div>
  );
}
