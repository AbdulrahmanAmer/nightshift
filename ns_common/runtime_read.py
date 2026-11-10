"""What `config/runtime.json` is, decided in ONE place for setup and for the product's commands.

Setup (`installer/stage2/runtime_cfg.py`, setup-torn-runtime) acts on the answer: it keeps, recodes, sets
aside or rebuilds the file. The commands an owner runs when something is wrong (`ns doctor`, `ns verify`,
`studio status`) only read it: `read_runtime` gives the settings when the file is `ok` and otherwise NS-E125
naming the file and what is wrong with it, so a damaged file is a row or a line, never a traceback, and the
commands and setup agree on what "damaged" means. Standard library, pydantic and `ns_common` only: setup and
every venv import it.

``inspect_runtime`` and ``Found`` moved here unchanged from `runtime_cfg` (FN-5/D-44 small item, block D);
`runtime_cfg` imports them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ns_common.atomic import read_bytes_retry
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths


@dataclass(frozen=True)
class Found:
    """What runtime.json is (``inspect_runtime``).

    ``ok``: it loads (``runtime`` is it). ``missing``. ``damaged``: not JSON, or a field this version knows
    has a wrong type or value (``why`` says which). ``newer``: the ONLY complaints are fields this version
    does not know, so a newer Nightshift wrote it: ``runtime`` is the file without exactly those fields and
    ``removed`` names them (dotted: ``llama.flags.x``). Both kinds of complaint together are ``damaged``.
    ``recode``: it reads fine but is not plain UTF-8 (a byte order mark, or UTF-16): every setting is kept and
    the file is written again as plain UTF-8, which is all the rest of Nightshift reads; ``why`` says which.
    ``unreadable``: it is there and cannot be read right now (another program holds it, no permission, a
    folder of that name): ``error`` is what the system said. Nothing is known about its settings.

    ``runtime`` is the one meaning of "the settings setup keeps from the existing file": set for ``ok``,
    ``recode`` and ``newer``, None for every other state. No step tests the state to decide that.
    """

    state: str
    runtime: Runtime | None = None
    why: str = ""
    removed: tuple[str, ...] = ()
    error: OSError | None = None


def _where(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(p) for p in loc) or "(the file)"


def _classify(text: str) -> Found:
    try:
        return Found("ok", Runtime.model_validate_json(text))
    except ValidationError as e:
        errors = e.errors()
    except ValueError as e:     # not decodable
        return Found("damaged", why=(str(e).splitlines() or [type(e).__name__])[0])
    if any(err["type"] == "json_invalid" for err in errors):
        return Found("damaged", why="not valid JSON: " + str(errors[0]["msg"]))
    unknown = [err for err in errors if err["type"] == "extra_forbidden"]
    wrong = [err for err in errors if err["type"] != "extra_forbidden"]
    wrong_text = "; ".join(f"{_where(err['loc'])}: {err['msg']}" for err in wrong)[:300]
    names = tuple(_where(err["loc"]) for err in unknown)
    if unknown and wrong:
        return Found(
            "damaged", removed=names,
            why=f"it has fields this version does not know ({', '.join(names)}) and values it cannot accept "
                f"({wrong_text})",
        )
    if wrong:
        return Found("damaged", why=wrong_text)
    try:
        data = json.loads(text)
        for err in unknown:
            node = data
            for step in err["loc"][:-1]:
                node = node[step]
            del node[err["loc"][-1]]
        return Found("newer", Runtime.model_validate(data), removed=names)
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as e:
        return Found("damaged", why=f"fields this version does not know ({', '.join(names)}) could not be "
                                    f"taken out: {type(e).__name__}")


#: A real runtime.json is a few kilobytes. A file over this is not one (garbage, a wrong file): not read.
MAX_BYTES = 1_000_000


def _decode(data: bytes) -> tuple[str, str]:
    """(text, how) of the file's bytes; how is empty for plain UTF-8. A UTF-8 byte order mark and UTF-16 with
    a byte order mark (what some Windows editors save) are unambiguous text: read, and the file is written as
    plain UTF-8 (``recode``), nothing lost. Any other bytes raise ``UnicodeDecodeError``."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "UTF-8 with a byte order mark"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "UTF-16"
    return data.decode("utf-8"), ""


def inspect_runtime(paths: Paths, read: Callable[[Path], bytes] = read_bytes_retry) -> Found:
    """What runtime.json is (see ``Found``). Every way reading it can go is one deliberate answer:

    - not there, or gone between two looks: ``missing``
    - plain UTF-8, UTF-8 with a byte order mark, UTF-16 with one: read (the last two are ``recode``)
    - other bytes, empty or whitespace only, not JSON, JSON that is not an object, over ``MAX_BYTES``, a known
      field with a bad value: ``damaged``
    - only unknown fields: ``newer``
    - a folder of that name, or a file that cannot be opened (another program holds it, no permission):
      ``unreadable``, which setup's ``existing_runtime`` and the commands' ``read_runtime`` turn into
      NS-E125; nothing is moved or rewritten then.

    It never raises for anything the file or the file system does.
    """
    path = paths.runtime_file
    try:
        if path.is_dir():
            raise IsADirectoryError(21, "is a folder")
        size = path.stat().st_size
        if size > MAX_BYTES:
            return Found("damaged", why=f"it is {size} bytes, far too large for a settings file")
        data = read(path)
    except FileNotFoundError:
        return Found("missing")
    except OSError as e:
        return Found("unreadable", why=f"{type(e).__name__}: {e}", error=e)
    if len(data) > MAX_BYTES:
        return Found("damaged", why=f"it is {len(data)} bytes, far too large for a settings file")
    try:
        text, how = _decode(data)
    except UnicodeDecodeError:
        return Found("damaged", why="it is not text this version can read (not UTF-8, and not UTF-16 with a "
                                    "byte order mark)")
    found = _classify(text)
    if found.state == "ok" and how:
        return Found("recode", found.runtime, why=how)
    return found


#: What each state of the file is, in NS-E125's detail (its hint says what to do: run setup again).
_SAYS = {
    "missing": "does not exist",
    "damaged": "could not be read: {why}",
    "newer": "was written by a newer Nightshift (settings this version does not know: {removed})",
    "recode": "is saved as {why}; Nightshift reads it only as plain UTF-8",
    "unreadable": "could not be read: {why} (if another program holds it, close that program first)",
}


def problem(paths: Paths, found: Found) -> str:
    """NS-E125's detail for a file that is not `ok`: the file's path and what is wrong with it."""
    says = _SAYS.get(found.state, "could not be read")
    return f"{paths.runtime_file} " + says.format(why=found.why, removed=", ".join(found.removed))


def read_runtime(paths: Paths) -> Runtime:
    """`runtime.json`'s settings, or NS-E125 naming the file and what is wrong with it (`inspect_runtime`
    decides: only `ok` is used as it is; a missing, damaged, newer, re-encoded or unreadable file raises).
    The file is never touched. Never raises anything but NsError."""
    found = inspect_runtime(paths)
    if found.state == "ok" and found.runtime is not None:
        return found.runtime
    raise NsError("NS-E125", problem(paths, found))
