"""C8 Clipper: error codes of spec 19.7 that are not yet in contracts/errors.json (CR-C8-0).

`register_codes()` adds them to the cached table of `ns_common.errors` with `setdefault`, so it becomes a
no-op once the orchestrator registers them in `contracts/errors.json`. Remove this module then.
"""
from __future__ import annotations

from ns_common.errors import ErrorDef, load_errors

CLIPPER_CODES: dict[str, tuple[str, str]] = {
    "NS-E405": (
        "Unsupported source video (too long, no audio, or unreadable).",
        "Use a video of at most 4 hours with an audio track that FFmpeg can open.",
    ),
    "NS-E406": (
        "Transcription produced fewer than 30 words.",
        "The audio is silent or music only. Check the source's audio track.",
    ),
    "NS-E407": (
        "No clip candidates passed the filters.",
        "Re-run selection with a wider length range or a higher max-clips value.",
    ),
    "NS-W402": (
        "The requested template does not apply to this video; using center_crop.",
        "No facecam or faces were found. Pick another template or leave it on auto.",
    ),
}


def register_codes() -> None:
    """Make the clipper codes known to `NsError` (idempotent)."""
    table = load_errors()
    for code, (message, hint) in CLIPPER_CODES.items():
        if code not in table:
            kind = "warning" if code.startswith("NS-W") else "error"
            table[code] = ErrorDef(code=code, message=message, hint=hint, kind=kind)  # type: ignore[arg-type]
