"""The one way Nightshift reads an upload sheet file (`.json` or `.txt` in the Outbox), and what it does next.

`read_sheet_file` never raises: it returns a `SheetRead` whose `state` is one of
- `OK`: read, decoded as UTF-8 (a UTF-8 byte-order mark is fine) and in the sheet's layout;
- `MISSING`: there is no such file;
- `HELD`: another program holds it (Windows sharing or lock violation);
- `NOT_UTF8`: saved as UTF-16 (with or without a byte-order mark) or in the PC's ANSI code page; `text` holds
  it decoded when that worked, and `value` the parsed sheet when the decoded text is a sheet;
- `TORN`: a `.json` that is empty or not valid JSON;
- `LAYOUT`: valid JSON that is not a sheet, or a `.txt` not laid out as Nightshift writes it;
- `OS_ERROR`: any other error opening or reading it, `detail` holding its text.

`plan_sheet` maps the pair (`.json` result, `.txt` result) to what happens (`Plan.action`) and the one
sentence the owner reads (`Plan.sentence`, None when nothing is asked of them). The table is in
`plan_sheet` itself and in the item's report (card posting-sheet-true, review of 149f3af).
"""

from __future__ import annotations

import codecs
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ns_common.atomic import retry_busy

OK, MISSING, HELD, NOT_UTF8, TORN, LAYOUT, OS_ERROR = (
    "ok", "missing", "held", "not_utf8", "torn", "layout", "os_error",
)
_SHARING = (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION: another program holds the file


@dataclass(frozen=True)
class SheetRead:
    """What reading one sheet file gave. `usable`: `value` holds the sheet (a model for `.json`, the parsed
    fields for `.txt`, the decoded text for `raw`)."""

    path: Path
    state: str
    text: str = ""
    raw: bytes = b""
    encoding: str = "utf-8"
    value: Any = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.value is not None and self.state in (OK, NOT_UTF8)


def ansi_codec() -> str:
    """The PC's ANSI code page, which Notepad's "ANSI" means (`mbcs` on Windows; cp1252 elsewhere)."""
    return "mbcs" if sys.platform == "win32" else "cp1252"


def _decode(raw: bytes) -> tuple[str | None, str]:
    """(text, encoding): UTF-8 (BOM allowed), else UTF-16 (BOM, or the NUL pattern of mostly-ASCII text
    without one), else the ANSI code page; (None, "unknown") when none of them decodes it."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for bom, enc in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            try:
                return raw[len(bom):].decode(enc), enc
            except UnicodeDecodeError:
                return None, "unknown"
    if len(raw) >= 4 and len(raw) % 2 == 0:
        odd, even = raw[1::2].count(0), raw[0::2].count(0)
        for zeros, enc in ((odd, "utf-16-le"), (even, "utf-16-be")):
            if zeros >= len(raw) // 4:  # half the characters or more are ASCII: NUL bytes on one side
                try:
                    return raw.decode(enc), enc
                except UnicodeDecodeError:
                    break
    try:
        return raw.decode(ansi_codec()), "ansi"
    except UnicodeDecodeError:
        return None, "unknown"


def read_sheet_file(path: Path, kind: Literal["json", "txt", "raw"]) -> SheetRead:
    """Read one sheet file: never raises (see the module text for the states)."""
    try:
        if path.is_dir():
            return SheetRead(path, OS_ERROR, detail="it is a folder, not a file")
        fh = retry_busy(lambda: open(path, "rb"))  # noqa: SIM115 (closed below, after the read)
    except FileNotFoundError:
        return SheetRead(path, MISSING)
    except PermissionError as e:  # the open refused: a sharing violation is another program; else access
        if getattr(e, "winerror", None) in _SHARING:
            return SheetRead(path, HELD, detail=str(e))
        return SheetRead(path, OS_ERROR, detail=str(e))
    except OSError as e:
        return SheetRead(path, OS_ERROR, detail=str(e))
    try:
        with fh:
            raw = fh.read()
    except PermissionError as e:  # opened, but the read refused: another program has locked the bytes
        return SheetRead(path, HELD, detail=str(e))
    except OSError as e:
        return SheetRead(path, OS_ERROR, detail=str(e))
    try:
        text, enc = _decode(raw)
        if text is None:
            return SheetRead(path, NOT_UTF8, raw=raw, encoding=enc, detail="not UTF-8, UTF-16 or ANSI")
        state = OK if enc == "utf-8" else NOT_UTF8
        text = text.replace("\r\n", "\n")
        if kind == "raw":
            return SheetRead(path, state, text, raw, enc, text)
        if kind == "txt":
            try:
                return SheetRead(path, state, text, raw, enc, parse_sheet_text(text))
            except ValueError as e:
                return SheetRead(path, LAYOUT if state == OK else NOT_UTF8, text, raw, enc, None, str(e))
        try:
            data = json.loads(text)
        except ValueError as e:
            return SheetRead(path, TORN if state == OK else NOT_UTF8, text, raw, enc, None, str(e))
        try:
            return SheetRead(path, state, text, raw, enc, sheet_model(data))
        except ValueError as e:
            return SheetRead(path, LAYOUT if state == OK else NOT_UTF8, text, raw, enc, None, str(e)[:300])
    except Exception as e:  # never raises: whatever else happens is reported, with its text
        return SheetRead(path, OS_ERROR, raw=raw, detail=f"{type(e).__name__}: {e}")


def sheet_model(data: Any) -> Any:
    """A `.json` sheet's model (`ClipMetadata` for a clip, else `Metadata`); ValueError when it is not one."""
    from ns_common.models import Metadata
    from ns_studio.clipper.models import ClipMetadata

    if not isinstance(data, dict):
        raise ValueError("not a sheet: the JSON is not an object")
    return (ClipMetadata if data.get("kind") == "clip" else Metadata).model_validate(data)


# ------------------------------------------------------------------------------------------ the .txt layout
_TITLE = re.compile(r"^TITLE: (.*)$", re.M)
_PUBLISH = re.compile(r"^PUBLISH AFTER \(local time\): (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", re.M)
_KIDS = re.compile(r"^  Made for kids: (Yes|No)$", re.M)
_AGE = re.compile(r"^  Age restriction suggested: (Yes|No)$", re.M)
_DISCLOSED = re.compile(r"^  Altered or synthetic content: (Yes|No|You decide)\b", re.M)
_RELATED = re.compile(r"^Related video: set Related video -> (.*)$", re.M)
_MIDROLL = re.compile(r"^MID-ROLL SUGGESTIONS \(chapter breaks\): (.*)$", re.M)
_SOURCE = re.compile(r"^  Source video: (.*) \(template (.*)\)$", re.M)
_HASHTAG_LINE = re.compile(r"^#\S+(?: #\S+)*$")


def parse_sheet_text(text: str) -> dict[str, Any]:
    """The fields of a `.txt` sheet as `sheet_text` and `clip_sheet_text` write them; ValueError when the
    text is not laid out that way. The description is everything between `DESCRIPTION:` and `TAGS:`, less
    the hashtag line, which is recognised by its own marker (one line of `#words` only), never by position:
    a description whose hashtag line was deleted keeps its last paragraph."""
    text = text.replace("\r\n", "\n")
    title, publish, kids, age, said = (r.search(text) for r in (_TITLE, _PUBLISH, _KIDS, _AGE, _DISCLOSED))
    start, tags_at = text.find("\nDESCRIPTION:\n"), text.rfind("\n\nTAGS: ")
    if not (title and publish and kids and age and said) or start < 0 or tags_at < start:
        raise ValueError("the .txt is not laid out as an upload sheet")
    block = text[start + len("\nDESCRIPTION:\n"):tags_at]
    head, _, last = block.rpartition("\n\n")
    if _HASHTAG_LINE.match(last.strip()):
        description, hashtags = head, last.split()
    elif not last.strip():  # an empty hashtag line
        description, hashtags = head, []
    else:
        description, hashtags = block.rstrip("\n"), []
    tags_line = text[tags_at + len("\n\nTAGS: "):].split("\n", 1)[0]
    related, midroll, source = _RELATED.search(text), _MIDROLL.search(text), _SOURCE.search(text)
    marks = [m.split(":") for m in midroll.group(1).split(", ")] if midroll else []
    return {
        "title": title.group(1), "description": description, "hashtags": hashtags,
        "tags": [t for t in tags_line.split(", ") if t],
        "publish_after": f"{publish.group(1)}T{publish.group(2)}",
        "made_for_kids": kids.group(1) == "Yes", "age_restrict_suggested": age.group(1) == "Yes",
        "ai_disclosure": "yes" if said.group(1) == "Yes" else "no",
        "related": related.group(1) if related else None,
        "midroll_suggestions_s": [int(m) * 60.0 + int(s) for m, s in marks] or None,
        "source": (source.group(1), source.group(2)) if source else None,
    }


# ------------------------------------------------------------------------------------------ the decision
RULES_TO_CHECK = (
    "no < or >, a title of 1 to 100 characters, a description of at most 5,000 bytes with its hashtags, "
    "and tags of at most 500 characters in all (a tag with a space counts two more)"
)
SAVE_AS_UTF8 = (
    "open {name} in Notepad, choose File > Save as, set Encoding to UTF-8, save, then open this video again"
)
AGAIN = "Nightshift tries again each time this video is opened and at its next start"

FIT = "fit"          # the .json is used: fitted, the .txt rewritten from it (the owner's text kept as needed)
REBUILD = "rebuild"  # the .json is rebuilt from the .txt and the job's records, then fitted
WAIT = "wait"        # nothing written: a file cannot be read now; tried again at the next start, on opening
GONE = "gone"        # nothing written: what the sheet held is not there to rebuild from


@dataclass(frozen=True)
class Plan:
    """What happens to a sheet, and the one sentence the owner reads (None: nothing is asked of them).
    `keep_txt`: the `.txt` on disk is the owner's and is about to be replaced: keep it as `.before-` first."""

    action: str
    sentence: str | None = None
    keep_txt: bool = False


def _copy_and_check(source: str) -> str:
    return (f"copy the title, description and tags from {source} into YouTube Studio and check them yourself "
            f"before posting: {RULES_TO_CHECK}")


def plan_sheet(j: SheetRead, t: SheetRead, record: Path | None, job_dir: Path) -> Plan:
    """The decision table: (`.json` result, `.txt` result) -> what happens and the owner's one sentence.

    Rows (`.json`): usable (OK, or another encoding decoded) | bad content (MISSING, TORN, LAYOUT, an
    encoding not decoded) | HELD | OS_ERROR. Columns (`.txt`): OK | another encoding decoded | MISSING |
    HELD | OS_ERROR | LAYOUT (an empty or torn `.txt` is another layout) | an encoding not decoded.
    - usable x OK, decoded, MISSING: FIT (a decoded `.txt` is kept as `.before-` and re-saved as UTF-8).
    - usable x LAYOUT, not decoded: FIT, the owner's file kept as `.before-` first.
    - usable x HELD, OS_ERROR: WAIT, the sentence names the `.txt`.
    - bad x OK, decoded: REBUILD (a decoded `.txt` kept as `.before-`).
    - bad x MISSING: GONE, the job's own SEO record named, else the fields are written by hand.
    - bad x HELD, OS_ERROR, not decoded: WAIT, the sentence names both files and what to do.
    - bad x LAYOUT: GONE, copy from the `.txt` and check against the rules.
    - HELD x any, OS_ERROR x any: WAIT, the sentence names the `.json` only (the `.txt` is read once it is
      free).
    Every sentence names only the files that failed and says what is true; none asks the owner to reject the
    video or to resume the job (a resume does not rewrite a finished job's sheets)."""
    jn, tn = j.path.name, t.path.name
    if j.state == HELD:
        return Plan(WAIT, f"{jn} is open in another program: close that program, then open this video again")
    if j.state == OS_ERROR:
        return Plan(WAIT, f"{jn} could not be opened ({j.detail}); {AGAIN}")
    if j.usable:
        if t.state in (OK, MISSING) or t.usable:
            return Plan(FIT, keep_txt=t.state == NOT_UTF8)
        if t.state == HELD:
            return Plan(WAIT, f"{tn} is open in another program, so Nightshift could not check it against "
                              "YouTube's rules: close that program, then open this video again")
        if t.state == OS_ERROR:
            return Plan(WAIT, f"{tn} could not be opened ({t.detail}), so Nightshift could not check it "
                              f"against YouTube's rules; {AGAIN}, and the Queue's copy buttons give the "
                              "checked fields meanwhile")
        return Plan(FIT, keep_txt=True)
    if t.usable:
        return Plan(REBUILD, keep_txt=t.state == NOT_UTF8)
    if t.state == HELD:
        return Plan(WAIT, f"{jn} could not be read; Nightshift rebuilds it from {tn}, which is open in "
                          "another program: close that program, then open this video again")
    if t.state == NOT_UTF8 and t.text == "":
        return Plan(WAIT, f"{jn} could not be read, and {tn} is saved in an encoding Nightshift cannot read: "
                          + SAVE_AS_UTF8.format(name=tn))
    if t.state == OS_ERROR:
        tail = f"; if it keeps failing, {_copy_and_check(str(record))}" if record else ""
        return Plan(WAIT, f"{jn} could not be read and {tn} could not be opened ({t.detail}); {AGAIN}{tail}")
    if t.state in (LAYOUT, NOT_UTF8):
        return Plan(GONE, f"{jn} could not be read and {tn} is not laid out as Nightshift writes it, so the "
                          f"sheet cannot be rebuilt: {_copy_and_check(tn)}")
    if record is not None:
        return Plan(GONE, f"{jn} could not be read and {tn} is missing, so Nightshift cannot rebuild it: the "
                          f"fields this job wrote are in {record}: {_copy_and_check(str(record))}")
    return Plan(GONE, f"{jn} could not be read, {tn} is missing and the job folder {job_dir} holds no SEO "
                      "record, so nothing is left to rebuild it from: write the title, description and tags "
                      f"in YouTube Studio yourself, keeping to: {RULES_TO_CHECK}")
