// Compile-time checks, no runtime code. If a StepId, CheckId or Phase is added to api/types.ts without a plain-words entry
// in copy.ts, `tsc` stops here (docs/design/installer-ui.md section 5.7).
import type { CheckId, Phase, StepId } from "./api/types";
import { checkLabel, phaseLabel, stepTitle } from "./copy";

type Keys<T> = keyof T;
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;
type Expect<T extends true> = T;

export type _StepsCovered = Expect<Same<Keys<typeof stepTitle>, StepId>>;
export type _ChecksCovered = Expect<Same<Keys<typeof checkLabel>, CheckId>>;
export type _PhasesCovered = Expect<Same<Exclude<Keys<typeof phaseLabel>, "welcome" | "uninstall" | "stopped">, Phase>>;
