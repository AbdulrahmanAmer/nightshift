"""Stub model service for supervisor tests (stands in for llama-server, embed and ns_tts).

Run: python stub_service.py --port N [--name X] [--ready-delay S] [--never-ready] [--die-after S]
     [--die-once-marker FILE] [--exit-immediately] [--tok-s F] [--spawn-child FILE]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE: dict[str, object] = {}


def _wav_bytes(seconds: float, sr: int = 24000) -> bytes:
    frames = int(seconds * sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x01" * frames)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: object) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            ready = (not STATE["never_ready"]) and time.monotonic() >= float(str(STATE["ready_at"]))
            if ready:
                self._json(200, {"status": "ok", "ok": True, "engine": "stub", "model_loaded": True})
            else:
                self._json(503, {"status": "loading model", "ok": False})
        elif self.path.startswith("/v1/voices"):
            self._json(
                200,
                [
                    {"name": "Ryan", "mode": "preset", "source": "stub"},
                    {"name": "MyClone", "mode": "clone", "source": "stub"},
                ],
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        if self.path == "/completion":
            n = int(body.get("n_predict", 16))
            tok_s = float(str(STATE["tok_s"]))
            self._json(
                200,
                {
                    "content": "stub " * min(n, 8),
                    "tokens_predicted": n,
                    "timings": {
                        "predicted_n": n,
                        "predicted_per_second": tok_s,
                        "prompt_per_second": tok_s * 10,
                    },
                },
            )
        elif self.path == "/v1/chat/completions":
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]})
        elif self.path == "/v1/embeddings":
            items = body.get("input") or [""]
            if isinstance(items, str):
                items = [items]
            data = [{"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]} for i, _ in enumerate(items)]
            self._json(200, {"data": data})
        elif self.path == "/v1/audio/speech":
            words = len(str(body.get("input", "")).split())
            self._send(200, _wav_bytes(max(0.5, words / 2.5)), "audio/wav")
        else:
            self._json(404, {"error": "not found"})


def _die_later(seconds: float) -> None:
    time.sleep(seconds)
    os._exit(3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="stub")
    ap.add_argument("--ready-delay", type=float, default=0.0)
    ap.add_argument("--never-ready", action="store_true")
    ap.add_argument("--die-after", type=float, default=None)
    ap.add_argument("--die-once-marker", default=None)
    ap.add_argument("--exit-immediately", action="store_true")
    ap.add_argument("--tok-s", type=float, default=42.0)
    ap.add_argument("--spawn-child", default=None, help="write the pid of a sleeping grandchild here")
    args, _unknown = ap.parse_known_args()

    if args.exit_immediately:
        print(f"stub {args.name} exiting immediately", file=sys.stderr, flush=True)
        sys.exit(5)

    die_after = args.die_after
    if args.die_once_marker:
        if os.path.exists(args.die_once_marker):
            die_after = None
        else:
            with open(args.die_once_marker, "w", encoding="utf-8") as fh:
                fh.write("died once")
            die_after = 0.3 if die_after is None else die_after

    if args.spawn_child:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(args.spawn_child, "w", encoding="utf-8") as fh:
            fh.write(str(child.pid))

    STATE["ready_at"] = time.monotonic() + args.ready_delay
    STATE["never_ready"] = args.never_ready
    STATE["tok_s"] = args.tok_s
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"stub {args.name} listening on {args.port}", flush=True)
    print(f"stub {args.name} warning line", file=sys.stderr, flush=True)
    if die_after is not None:
        threading.Thread(target=_die_later, args=(die_after,), daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
