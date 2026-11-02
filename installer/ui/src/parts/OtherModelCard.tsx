// The "Other" card of the model picker: a Hugging Face repository and GGUF file name the owner types. When the owner leaves
// a field, the server looks the file up (HEAD request), refuses anything over 15.5 GB and returns its size, which is shown here.
import { Badge } from "@ref12";
import { choices as t } from "../copy";
import { gbNum, vramLeftGb } from "../format";
import { LabeledField } from "./LabeledField";
import { cardClass, vramLine } from "./ModelPicker";

export type ModelLookup = { status: "idle" | "checking" | "ok" | "bad"; key?: string; size?: number; message?: string };

export function OtherModelCard({ selected, disabled, repo, file, lookup, error, onSelect, onRepo, onFile, onCheck }: {
  selected: boolean; disabled: boolean; repo: string; file: string; lookup: ModelLookup; error?: string;
  onSelect: () => void; onRepo: (v: string) => void; onFile: (v: string) => void; onCheck: () => void;
}) {
  const o = t.picker.other;
  const vram = lookup.status === "ok" && lookup.size ? vramLine(vramLeftGb(lookup.size)) : null;
  const note = lookup.status === "checking"
    ? <span className="text-ds-text-2">{o.checking}</span>
    : lookup.status === "ok" && lookup.size
      ? <><span className="font-medium">{t.picker.download(gbNum(lookup.size))}</span>{vram && <span className="text-ds-text-2">{vram}</span>}</>
      : lookup.status === "bad"
        ? null
        : <span className="text-ds-muted">{o.hint}</span>;
  return (
    <div className={cardClass(selected, disabled)} onFocusCapture={() => { if (!selected && !disabled) onSelect(); }}>
      <label className="flex cursor-pointer items-center justify-between gap-2">
        <input type="radio" name="llm-model" className="sr-only" checked={selected} disabled={disabled} onChange={onSelect} />
        <span className="text-ds-md leading-ds-snug font-semibold">{o.name}</span>
        {lookup.status === "ok" && <Badge tone="success" size="sm">{o.found}</Badge>}
      </label>
      <p className="text-ds-xs leading-ds-normal text-ds-text-2">{o.about}</p>
      <LabeledField label={o.repo} placeholder={o.repoPh} value={repo} disabled={disabled} spellCheck={false} autoComplete="off" maxLength={140}
        onChange={(e) => onRepo(e.target.value)} onBlur={onCheck} inputClassName="font-mono text-ds-xs" />
      <LabeledField label={o.file} placeholder={o.filePh} value={file} disabled={disabled} spellCheck={false} autoComplete="off" maxLength={240}
        onChange={(e) => onFile(e.target.value)} onBlur={onCheck} inputClassName="font-mono text-ds-xs"
        error={lookup.status === "bad" ? lookup.message : error} />
      {note && <span className="flex flex-col gap-0.5 text-ds-xs ds-num">{note}</span>}
    </div>
  );
}
