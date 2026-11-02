/** An API failure with the envelope's parts. `message` is "reason. hint" (what toasts show); `code` is e.g. NS-E312 or consent_required. */
export class ApiError extends Error {
  constructor(message: string, readonly code?: string, readonly reason: string = message, readonly hint?: string) { super(message); }
}
