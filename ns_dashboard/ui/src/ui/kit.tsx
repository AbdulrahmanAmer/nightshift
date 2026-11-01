import { useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Check, Clapperboard, MonitorPlay, Scissors, Smartphone, Upload } from "lucide-react";

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

type Variant = "primary" | "accent" | "secondary" | "ghost" | "danger" | "glass";
type Size = "sm" | "md" | "lg";
const V: Record<Variant, string> = {
  primary: "bg-fill text-on-fill hover:opacity-90",
  accent: "bg-accent text-on-accent hover:brightness-[0.96]",
  secondary: "bg-card text-text border border-line-2 hover:bg-raised",
  ghost: "text-text-2 hover:text-text hover:bg-raised",
  danger: "bg-card text-bad border border-line-2 hover:bg-bad-soft",
  glass: "glass-chip text-[var(--chip-glass-text)] hover:opacity-90",
};
const S: Record<Size, string> = { sm: "h-8 px-3 text-[13px] gap-1.5", md: "h-10 px-4 text-[14px] gap-2", lg: "h-12 px-5 text-[15px] gap-2" };
export function Button({ variant = "secondary", size = "md", className, icon, children, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: Size; icon?: ReactNode }) {
  return (
    <button type="button" {...rest} className={cx("inline-flex shrink-0 items-center justify-center rounded-full font-medium whitespace-nowrap select-none transition-[background-color,color,opacity,transform,filter] duration-150 ease-out active:scale-[0.98] disabled:pointer-events-none disabled:opacity-40", V[variant], S[size], className)}>
      {icon}{children}
    </button>
  );
}
export function LinkButton({ href, variant = "secondary", size = "md", className, icon, children, external }: { href: string; variant?: Variant; size?: Size; className?: string; icon?: ReactNode; children: ReactNode; external?: boolean }) {
  return (
    <a href={href} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined} className={cx("inline-flex shrink-0 items-center justify-center rounded-full font-medium whitespace-nowrap no-underline transition-[background-color,color,opacity] duration-150 ease-out", V[variant], S[size], className)}>
      {icon}{children}
    </a>
  );
}
export function IconButton({ label, children, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return <button type="button" aria-label={label} title={label} {...rest} className={cx("inline-flex size-9 shrink-0 items-center justify-center rounded-full text-text-2 transition-colors duration-150 hover:bg-raised hover:text-text", className)}>{children}</button>;
}

export function Card({ className, children, as: As = "section", ...rest }: { className?: string; children: ReactNode; as?: any; [k: string]: any }) {
  return <As {...rest} className={cx("card", className)}>{children}</As>;
}
export function CardHead({ title, sub, right, className }: { title: ReactNode; sub?: ReactNode; right?: ReactNode; className?: string }) {
  return (
    <div className={cx("flex items-start justify-between gap-3", className)}>
      <div className="min-w-0"><h2 className="text-[17px] leading-6 font-medium">{title}</h2>{sub && <p className="mt-0.5 text-[13px] text-muted">{sub}</p>}</div>
      {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
    </div>
  );
}
export function PageHeader({ title, sub, right }: { title: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
      <div className="min-w-0"><h1 className="text-[30px] leading-9 font-medium">{title}</h1>{sub && <p className="mt-1 text-[14px] text-text-2">{sub}</p>}</div>
      {right && <div className="flex max-w-full min-w-0 flex-wrap items-center gap-2">{right}</div>}
    </header>
  );
}

export function SegTabs<T extends string>({ value, onChange, items, label, size = "md" }: { value: T; onChange: (v: T) => void; items: { id: T; label: string; count?: number }[]; label: string; size?: "sm" | "md" }) {
  return (
    <div role="tablist" aria-label={label} className="inline-flex max-w-full gap-1 overflow-x-auto rounded-full border border-line bg-card p-1 shadow-sm scroll-thin">
      {items.map((it) => {
        const on = it.id === value;
        return (
          <button key={it.id} role="tab" type="button" aria-selected={on} onClick={() => onChange(it.id)}
            className={cx("inline-flex items-center gap-2 rounded-full whitespace-nowrap transition-[background-color,color] duration-150", size === "sm" ? "h-8 px-3 text-[13px]" : "h-9 px-4 text-[14px]", on ? "bg-fill text-on-fill" : "text-text-2 hover:bg-raised hover:text-text")}>
            {it.label}
            {it.count != null && <span className={cx("font-mono text-[12px] num", on ? "opacity-70" : "text-muted")}>{it.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function Toggle({ on, onChange, label, size = "lg" }: { on: boolean; onChange: (v: boolean) => void; label: string; size?: "md" | "lg" }) {
  const big = size === "lg";
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label} onClick={() => onChange(!on)}
      className={cx("relative shrink-0 rounded-full border transition-colors duration-150", big ? "h-8 w-14" : "h-6 w-10", on ? "border-transparent bg-fill" : "border-line-2 bg-sunken")}>
      <span className={cx("absolute top-1/2 -translate-y-1/2 rounded-full shadow-sm transition-transform duration-200 ease-out", big ? "left-1 size-6" : "left-0.5 size-[18px]", on ? (big ? "translate-x-6 bg-on-fill" : "translate-x-4 bg-on-fill") : "bg-card")} />
    </button>
  );
}

export function CheckChip({ on, onChange, children, warn }: { on: boolean; onChange: (v: boolean) => void; children: ReactNode; warn?: boolean }) {
  return (
    <button type="button" aria-pressed={on} onClick={() => onChange(!on)}
      className={cx("inline-flex h-9 items-center gap-2 rounded-full border px-3.5 text-[13px] transition-colors duration-150", on ? "border-transparent bg-fill text-on-fill" : cx("bg-card text-text-2 hover:text-text", warn ? "border-bad/40" : "border-line-2"))}>
      {on && <Check size={14} strokeWidth={1.5} aria-hidden />}{children}
    </button>
  );
}

export function Keycap({ children }: { children: ReactNode }) {
  return <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] border border-line-2 bg-card px-1 font-mono text-[11px] leading-none text-text-2 shadow-[inset_0_-1px_0_var(--line-2)]">{children}</kbd>;
}

export function Progress({ value, tone = "accent", className }: { value: number; tone?: "accent" | "ok" | "bad" | "muted"; className?: string }) {
  const c = { accent: "bg-accent", ok: "bg-ok", bad: "bg-bad", muted: "bg-muted" }[tone];
  return <div className={cx("h-2 overflow-hidden rounded-full bg-sunken", className)} role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}><div className={cx("h-full rounded-full", c)} style={{ width: `${Math.max(2, Math.min(100, value))}%` }} /></div>;
}

export function Field({ label, hint, children, className, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; className?: string; htmlFor?: string }) {
  return (
    <div className={cx("flex min-w-0 flex-col gap-1.5", className)}>
      <label htmlFor={htmlFor} className="text-[13px] font-medium text-text-2">{label}</label>
      {children}
      {hint && <p className="text-[12px] leading-4 text-muted">{hint}</p>}
    </div>
  );
}

export function Empty({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-[20px] border border-dashed border-line-2 px-6 py-12 text-center">
      {icon && <div className="mb-1 text-muted">{icon}</div>}
      <p className="text-[15px] font-medium">{title}</p>
      {children && <p className="max-w-md text-[13px] text-text-2">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function KindTag({ kind, className }: { kind: string; className?: string }) {
  const I = kind === "long" ? MonitorPlay : kind === "short" ? Smartphone : kind === "clip" ? Scissors : Clapperboard;
  const label = ({ long: "Full video", short: "Short", clip: "Clip" } as Record<string, string>)[kind] || kind;
  return <span className={cx("inline-flex items-center gap-1.5 text-[12px] text-text-2", className)}><I size={14} strokeWidth={1.5} aria-hidden />{label}</span>;
}

export function StatusDot({ tone }: { tone: "ok" | "warn" | "bad" | "muted" | "accent" }) {
  const c = { ok: "bg-ok", warn: "bg-accent", bad: "bg-bad", muted: "bg-muted", accent: "bg-accent" }[tone];
  return <span aria-hidden className={cx("inline-block size-2 shrink-0 rounded-full", c)} />;
}

export function Dropzone({ title, sub, accept, onFile, compact }: { title: string; sub: string; accept: string; onFile: (f: File) => void; compact?: boolean }) {
  const [over, setOver] = useState(false); const input = useRef<HTMLInputElement>(null);
  return (
    <label onDragEnter={(e) => { e.preventDefault(); setOver(true); }} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files?.[0]; if (f) onFile(f); }}
      className={cx("flex cursor-pointer flex-col items-center justify-center gap-2 rounded-[16px] border-2 border-dashed text-center transition-colors duration-150", compact ? "px-4 py-6" : "px-6 py-10", over ? "border-accent bg-accent-soft" : "border-line-2 bg-raised/60 hover:bg-raised")}>
      <span className="flex size-10 items-center justify-center rounded-full bg-card text-text-2 shadow-sm"><Upload size={18} strokeWidth={1.5} aria-hidden /></span>
      <span className="text-[15px] font-medium">{title}</span>
      <span className="max-w-md text-[13px] text-text-2">{sub}</span>
      <span className="mt-1 inline-flex h-8 items-center rounded-full border border-line-2 bg-card px-3 text-[13px] font-medium">Choose a file</span>
      <input ref={input} type="file" accept={accept} className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ""; }} />
    </label>
  );
}

export function Skeleton({ className }: { className?: string }) { return <div className={cx("rounded-[12px] bg-sunken", className)} />; }
