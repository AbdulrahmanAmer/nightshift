"""F-D3: Nightshift Code follows `llama.port`: the half that the dashboard owns.

Settings' engine-settings edit (PUT /api/settings?unlock=true {flags: {llama.port: N}}) must change the port
the engine reads, `config/runtime.json` `llama.port`, and GET /api/settings must read the new port back. The
proof sets another port, reads both, and puts the original port back the same way (also when a check fails).

What the box is really about happens after that and cannot run from a proof file: the engine restarts, then
the "Nightshift Code" launcher (launchers/nightshift-code.ps1) starts llama on the new port, rewrites
`config/opencode.json` baseURL from the endpoint the engine reports, and starts OpenCode. That part ends as
skipped. Ported from runs/qa/fd3.ps1 (evidence log 2026-11-09 19:51, 0.1.14, engine install).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofs._lib import short

PROOF = {
    "id": "f-d3-code-follows-llama-port",
    "boxes": ["F-D3"],
    "install": "engine",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 300,
    "fakes": [],
}


def _runtime_port(ctx: Any) -> Any:
    """`llama.port` as `config/runtime.json` holds it on disk."""
    data = json.loads((ctx.home / "config" / "runtime.json").read_text(encoding="utf-8"))
    return (data.get("llama") or {}).get("port")


def _base_url(ctx: Any) -> str | None:
    """The baseURL in config/opencode.json, None when the install has no such file."""
    f: Path = ctx.home / "config" / "opencode.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))["provider"]["local"]["options"]["baseURL"]
    except (ValueError, KeyError, TypeError):
        return None


def _put_port(ctx: Any, port: int) -> tuple[int, Any]:
    return ctx.api("PUT", "/api/settings?unlock=true", {"flags": {"llama.port": port}})


def run(ctx: Any) -> None:
    status, settings = ctx.api("GET", "/api/settings")
    original = (settings.get("flags") or {}).get("llama.port") if isinstance(settings, dict) else None
    ctx.check(
        status == 200 and isinstance(original, int) and original == _runtime_port(ctx),
        "GET /api/settings shows the llama.port that runtime.json holds",
        status=status,
        settings_port=original,
        file_port=_runtime_port(ctx),
    )
    changed = 8082 if original != 8082 else 8083
    url_before = _base_url(ctx)
    ctx.step("opencode.json before the edit", base_url=url_before)
    try:
        status, answer = _put_port(ctx, changed)
        flags = answer.get("flags") if isinstance(answer, dict) else None
        ctx.check(
            status == 200 and (flags or {}).get("llama.port") == changed,
            "PUT /api/settings?unlock=true {flags: {llama.port}} answers the new port",
            status=status,
            new_port=changed,
            answer=short(flags if flags is not None else answer),
        )
        ctx.check(
            _runtime_port(ctx) == changed,
            "config/runtime.json holds the new llama.port on disk",
            file_port=_runtime_port(ctx),
        )
        status, settings = ctx.api("GET", "/api/settings")
        ctx.check(
            (settings.get("flags") or {}).get("llama.port") == changed,
            "GET /api/settings reads the new port back",
            settings_port=(settings.get("flags") or {}).get("llama.port"),
        )
        ctx.step(
            "opencode.json right after the edit: the launcher, not the dashboard, rewrites it",
            base_url=_base_url(ctx),
            unchanged=_base_url(ctx) == url_before,
        )
    finally:
        status, answer = _put_port(ctx, int(original) if isinstance(original, int) else 8080)
        ctx.step("original llama.port put back", status=status, file_port=_runtime_port(ctx))
    ctx.check(
        _runtime_port(ctx) == original,
        "the original llama.port is back in runtime.json",
        file_port=_runtime_port(ctx),
        original=original,
    )

    ctx.skip(
        "the launcher half of F-D3 is not exercised: after the port edit the engine must be restarted "
        "(llama only reads llama.port at start), then launchers/nightshift-code.ps1 must run, acquire llama "
        "on the new port, rewrite config/opencode.json baseURL to http://127.0.0.1:<port>/v1 and start "
        "OpenCode, and the lease must be released when OpenCode ends. A proof file cannot restart the engine "
        "or start a PowerShell launcher with a stand-in opencode.exe on PATH. The runner would need "
        "(1) an engine-restart primitive (nsup after nsproc -Down) between the edit and the launch, and "
        "(2) a launcher primitive that runs nightshift-code.ps1 with a stand-in opencode.exe and returns its "
        "pid so the proof can end it, then read GET <port>/v1/models, opencode.json and the llama owners "
        "in the supervisor status"
    )
