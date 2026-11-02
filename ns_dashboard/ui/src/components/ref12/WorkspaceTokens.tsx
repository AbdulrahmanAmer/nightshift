import { Icon, type IconName } from "./Icon";

/** WorkspaceTokens (Planify, made generic): the pair of 30 px round accent-soft tokens after the salutation,
 *  overlapping 6 px with a 3 px app-colour halo. Each token has a label for assistive tech. */
export function WorkspaceTokens({ tokens }: { tokens: { icon: IconName; label: string }[] }) {
  return (
    <span className="inline-flex">
      {tokens.map((t, i) => (
        <span key={t.label} role="img" aria-label={t.label} title={t.label}
          className={`inline-grid size-[30px] place-items-center rounded-full bg-ds-accent-soft text-ds-accent-ink shadow-[0_0_0_3px_var(--ds-bg-app)] ${i ? "-ml-1.5" : ""}`}>
          <Icon name={t.icon} size={14} strokeWidth={2.2} />
        </span>
      ))}
    </span>
  );
}
