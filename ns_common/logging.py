from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ns_common.paths import Paths

Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class NsLogger:
    def __init__(
        self,
        component: str,
        logs_dir: Path,
        *,
        echo: bool | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.component = component
        self.logs_dir = logs_dir
        self.echo = (os.environ.get("NS_LOG_STDERR") == "1") if echo is None else echo
        self.clock = clock or (lambda: datetime.now(UTC))
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def file_for(self, when: datetime) -> Path:
        return self.logs_dir / f"{self.component}-{when:%Y%m%d}.jsonl"

    def log(self, level: Level, event: str, msg: str = "", **data: Any) -> None:
        now = self.clock()
        rec = {
            "ts": now.isoformat(),
            "level": level,
            "component": self.component,
            "event": event,
            "msg": msg,
            "data": data,
        }
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self.file_for(now).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.echo:
            extra = json.dumps(data, default=str)
            print(f"[{level}] {self.component} {event} {msg} {extra}", file=sys.stderr)

    def debug(self, event: str, msg: str = "", **data: Any) -> None:
        self.log("DEBUG", event, msg, **data)

    def info(self, event: str, msg: str = "", **data: Any) -> None:
        self.log("INFO", event, msg, **data)

    def warn(self, event: str, msg: str = "", **data: Any) -> None:
        self.log("WARN", event, msg, **data)

    def error(self, event: str, msg: str = "", **data: Any) -> None:
        self.log("ERROR", event, msg, **data)

    @contextmanager
    def timed(self, event: str, **data: Any) -> Iterator[None]:
        t0 = time.perf_counter()
        self.info(f"{event}_start", **data)
        try:
            yield
        finally:
            self.info(f"{event}_end", duration_s=round(time.perf_counter() - t0, 3), **data)


def get_logger(component: str, paths: Paths) -> NsLogger:
    return NsLogger(component, paths.logs)
