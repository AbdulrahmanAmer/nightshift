// WebView2 bridge (docs/design/installer-ui.md section 6.3). The bridge itself is shared with the dashboard; this file only
// re-exports it and adds the one command the setup window needs on top (open-external, handled by nightshift-host.ps1).
import { host as dashHost, type HostCommand, type HostEvent } from "@dash/lib/host";

export type { HostCommand, HostEvent };
export const host = dashHost;

/** Open an address in the default browser: through the host when there is one, otherwise a new tab. */
export function openExternal(url: string): void {
  if (host.available()) host.send({ cmd: "open-external", url } as unknown as HostCommand);
  else window.open(url, "_blank", "noopener");
}
