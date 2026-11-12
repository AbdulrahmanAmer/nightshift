"""The Assistant's stories as draft files: one Markdown file per story in `<home>\\drafts\\` (G-43 b, G-47).

The owner planned a long video with the Assistant; the story was re-typed into one tool call, cut by the reply
cap and lost: it existed only inside `data/assistant.db`. A story is now a file in the owner's reach:

- `draft_create` makes `<home>/drafts/<id>.md` (front-matter: title, channel, format, status), `draft_append_part`
  adds ONE part per call (each call well under the reply cap), `draft_read` reads it back, and
  `start_job_from_draft` starts the job FROM THE FILE: the server reads it when the owner presses Yes, so no story
  text ever travels inside a reply that can be cut, and the owner's own edits to the file are what is narrated.
- Every write reads the file again first (the owner may edit it by hand between parts) and is atomic; a write that
  races an editor's save is retried, never merged blindly.
- A reply cut by the cap writes nothing: the call never ran. The cut note names the draft and how many parts it
  holds (`cut_note`).
- Headings are never narrated. Every heading line (the `## Part N: Title` lines this module writes, any other
  Markdown heading, and label lines such as "Part 2: The Descent (Mara POV)" that the script check refuses inside
  narration) is LEFT OUT of the text the job gets; Nightshift cuts the chapters itself at paragraph ends. The lines
  left out are listed in the result so the owner sees them.
- The Open button opens only a `.md` file directly inside `drafts\\` (`open_draft`).
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ns_common.atomic import create_text_atomic, write_text_atomic
from ns_common.models import SCRIPT_MAX_CHARS
from ns_common.textstats import word_count
from ns_dashboard.errors import AdapterUnavailable, ApiError

if TYPE_CHECKING:
    from ns_common.paths import Paths
    from ns_dashboard.assistant.registry import ToolContext

DIR_NAME = "drafts"
#: One part per call stays well under the reply cap (1,200 tokens at 16K): about 500 words is about 700 tokens
#: of text plus the JSON around it. The schema says so to the model; this module refuses more.
PART_MAX_CHARS = 4000
PART_WORDS_HINT = 500
MAX_PARTS = 40
SCAN_LIMIT = 200  # drafts read when looking for one by conversation or by text (newest first)
WRITE_TRIES = 3
FORMATS = ("shorts_only", "master_first")
STATUSES = ("writing", "started", "refused")
ABOUT = ("Your story draft. Edit it freely in any text editor. Lines that start with # are headings: they are "
         "not read aloud. Nightshift reads this file again when the video is started.")

#: A draft id is the file name without `.md`: letters, digits, spaces, `-` and `_`; no dots, no slashes.
ID_RE = re.compile(r"^[\w][\w -]{0,99}$")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_FM = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)$", re.S)
PART_HEAD = re.compile(r"^##[ \t]+Part[ \t]+\d+\b[ \t]*[:.)\-]?[ \t]*(.*?)[ \t]*$", re.M | re.I)
_MD_HEAD = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]|$)")
_LABEL = re.compile(
    r"^[*_ \t]*(?:part|chapter|act|episode|scene)[ \t]+"
    r"(?:\d+|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.I,
)
_RULE = re.compile(r"^[ \t]*(?:[-*_][ \t]*){3,}$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_ENDS_SENTENCE = (".", "!", "?", '"', "”", "…", "'")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    key = str(path).lower()
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------- text rules


def heading_line(line: str) -> bool:
    """True for a line that is a heading, not narration: a Markdown heading, or a short label line such as
    "Part 2: The Descent (Mara POV)" or "**Chapter 3**" that does not end like a sentence."""
    s = line.strip()
    if not s:
        return False
    if _MD_HEAD.match(line):
        return True
    if _LABEL.match(s):
        bare = s.rstrip("*_ \t")
        return word_count(bare) <= 14 and not bare.endswith(_ENDS_SENTENCE)
    return False


def script_text(body: str) -> tuple[str, list[str]]:
    """The narration of a draft body and the heading lines left out of it.

    Comments are dropped, every heading line and every scene-break rule (`---`, `* * *`) becomes a paragraph
    break, and runs of blank lines collapse to one. Nightshift cuts the chapters itself (`script_markdown`).
    """
    text = _COMMENT.sub("", body.replace("\r\n", "\n").replace("\r", "\n"))
    kept: list[str] = []
    left_out: list[str] = []
    for line in text.split("\n"):
        if heading_line(line):
            left_out.append(line.strip())
            kept.append("")
        elif _RULE.match(line):
            kept.append("")
        else:
            kept.append(line.rstrip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return out, left_out


def _clean_part(text: str) -> tuple[str, str]:
    """A part's text without a heading the model put on top of it; returns (heading found or "", text)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    head = ""
    while lines and (not lines[0].strip() or heading_line(lines[0])):
        if lines[0].strip() and not head:
            head = lines[0].strip().lstrip("#").strip().strip("*_").strip()
        lines.pop(0)
    return head, "\n".join(lines).strip()


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:50].rstrip("-") or "story"


# --------------------------------------------------------------------------- files


def drafts_dir(paths: Paths) -> Path:
    """`<home>/drafts`: one Markdown file per story the Assistant writes (the owner's to read and edit)."""
    return paths.home / DIR_NAME


@dataclass
class Draft:
    """A draft file as read now: front-matter, body, and where it lives."""

    draft_id: str
    path: Path
    meta: dict[str, Any]
    body: str
    meta_ok: bool = True  # False when the owner's edit broke the YAML block (the body is still used)
    raw_head: str = ""    # the block as written, kept word for word when it cannot be read

    def parts(self) -> list[tuple[str, str]]:
        """(title, text) per part; text before the first `## Part` heading is a part of its own."""
        heads = list(PART_HEAD.finditer(self.body))
        out: list[tuple[str, str]] = []
        lead = self.body[: heads[0].start()] if heads else self.body
        if script_text(lead)[0]:
            out.append(("", lead.strip()))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(self.body)
            out.append((h.group(1).strip(), self.body[h.end():end].strip()))
        return out

    def narration(self) -> tuple[str, list[str]]:
        """The text the job narrates and the heading lines left out of it."""
        return script_text(self.body)


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # saved by an editor in the Windows code page: read it, never refuse it
        text = raw.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split(text: str) -> tuple[dict[str, Any], str, bool, str]:
    """(front-matter, body, readable, the block as written)."""
    m = _FM.match(text)
    if not m:
        return {}, text, True, ""
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, m.group(2), False, m.group(1)
    if not isinstance(meta, dict):
        return {}, m.group(2), False, m.group(1)
    return meta, m.group(2), True, m.group(1)


def _join(meta: dict[str, Any], body: str, raw_head: str | None = None) -> str:
    """The file text. `raw_head` (an unreadable block of the owner's) is written back word for word; a file
    without any block gets none."""
    body = body.strip("\n")
    if raw_head is not None:
        return f"---\n{raw_head}\n---\n\n{body}\n"
    if not meta:
        return f"{body}\n"
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100)
    return f"---\n{head.rstrip()}\n---\n\n{body}\n"


def _read(path: Path) -> Draft:
    meta, body, ok, raw = _split(_decode(path.read_bytes()))
    return Draft(path.stem, path, meta, body, ok, raw)


def _part_title(text: str) -> str:
    """A part's heading without a "Part 2:" label of its own (the file numbers the parts)."""
    t = " ".join(str(text or "").split()).strip("#*_ ")
    if re.fullmatch(r"(?:part|chapter)[ \t]+\w+[ \t]*[:.)\-]*", t, flags=re.I):
        return ""
    t = re.sub(r"^(?:part|chapter)[ \t]+\w+[ \t]*[:.)\-]+[ \t]*", "", t, flags=re.I)
    return t[:100]


def _head(n: int, title: str) -> str:
    """A part's heading line in the file (never narrated)."""
    return f"## Part {n}: {title}" if title else f"## Part {n}"


def _bad_ref(ref: str, root: Path) -> ApiError:
    return ApiError(422, "bad_draft", f"'{ref}' is not a draft. Drafts are the .md files in {root}; name one by its "
                                      "id (the file name without .md) or its full path.")


def resolve(paths: Paths, ref: Any) -> Path:
    """The draft file named by `ref` (an id, `<id>.md`, or a full path inside the drafts folder).

    Refuses anything that is not a `.md` file directly inside `<home>/drafts` once every link is followed.
    """
    root = drafts_dir(paths)
    raw = str(ref or "").strip().strip('"').strip("'").strip()
    if not raw:
        raise ApiError(422, "bad_draft", f"Name the draft: its id or its path in {root}.")
    if "/" in raw or "\\" in raw or ":" in raw:
        given = Path(raw)
        try:
            same = given.resolve().parent == root.resolve()
        except (OSError, RuntimeError, ValueError):
            same = False
        if not same:
            raise ApiError(403, "outside_drafts", f"Only drafts in {root} can be used here, not '{raw}'.")
        raw = given.name
    stem = raw[:-3] if raw.lower().endswith(".md") else raw
    if not ID_RE.match(stem) or stem.lower().split(" ")[0] in _RESERVED or stem != stem.strip():
        raise _bad_ref(str(ref), root)
    path = root / f"{stem}.md"
    try:
        inside = path.resolve().parent == root.resolve()
    except (OSError, RuntimeError, ValueError):
        inside = False
    if not inside:
        raise ApiError(403, "outside_drafts", f"Only drafts in {root} can be used here, not '{raw}'.")
    if not path.is_file():
        raise ApiError(404, "draft_not_found", f"There is no draft '{stem}' in {root}.",
                       hint="list_drafts shows the drafts that exist.")
    return path


def load(paths: Paths, ref: Any) -> Draft:
    """Read the draft `ref` from disk now (never from memory: the owner may have edited it)."""
    path = resolve(paths, ref)
    try:
        return _read(path)
    except OSError as e:
        raise ApiError(409, "draft_unreadable", f"The draft {path} cannot be read right now ({e}). "
                                                "If it is open in an editor, save and close it, then try again.") from e


def card(d: Draft) -> dict[str, Any]:
    """What the chat card and the model see of a draft: where it is, how long it is, its parts."""
    text, _ = d.narration()
    out: dict[str, Any] = {
        "draft_id": d.draft_id, "path": str(d.path), "title": str(d.meta.get("title") or d.draft_id),
        "channel": d.meta.get("channel"), "format": d.meta.get("format"),
        "status": str(d.meta.get("status") or "writing"), "words": word_count(text),
        "parts": [{"n": i, "title": t or "(no heading)", "words": word_count(script_text(p)[0])}
                  for i, (t, p) in enumerate(d.parts(), start=1)],
    }
    if d.meta.get("job_id"):
        out["job_id"] = d.meta["job_id"]
    if not d.meta_ok:
        out["warning"] = ("The block between the --- lines at the top of the file is not valid any more; "
                          "the story text is still used.")
    return out


def _write(path: Path, change: Callable[[Draft], tuple[dict[str, Any], str] | None]) -> Draft:
    """Read the file, apply `change`, write it back atomically; retried when the file changed in between (the
    owner saved it in an editor). `change` returning None writes nothing. Returns the draft as written."""
    with _lock_for(path):
        for _ in range(WRITE_TRIES):
            try:
                before = path.stat()
                d = _read(path)
            except OSError as e:
                raise ApiError(409, "draft_unreadable", f"The draft {path} cannot be read right now ({e}).") from e
            changed = change(d)
            if changed is None:
                return d
            meta, body = changed
            try:
                now = path.stat()
            except OSError:
                continue
            if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
                continue  # the owner saved the file meanwhile: start again from his version
            try:
                write_text_atomic(path, _join(meta, body, None if d.meta_ok else d.raw_head))
            except OSError as e:
                raise ApiError(409, "draft_locked", f"The draft {path} could not be saved ({e}). Nothing in it was "
                                                    "changed. If it is open in an editor, close it and try again.") from e
            return Draft(d.draft_id, path, meta, body, d.meta_ok, d.raw_head)
    raise ApiError(409, "draft_busy", f"The draft {path} kept changing while I wrote to it. Nothing in it was "
                                      "changed by me. Save it in your editor, then ask again.")


def _new_id(root: Path, title: str, now: datetime) -> str:
    base = f"{now:%Y%m%d}-{_slug(title)}"
    for n in range(1, 1000):
        stem = base if n == 1 else f"{base}-{n}"
        if not (root / f"{stem}.md").exists():
            return stem
    raise ApiError(409, "too_many_drafts", f"There are too many drafts called '{base}' in {root}.")


def create(paths: Paths, *, title: str, channel: str, fmt: str, conversation_id: str = "",
           parts: list[tuple[str, str]] | None = None, status: str = "writing") -> Draft:
    """A new draft file (never replaces one): front-matter plus the given parts."""
    root = drafts_dir(paths)
    title = " ".join(str(title or "").split())[:120] or "Untitled story"
    now = datetime.now().astimezone()
    meta: dict[str, Any] = {"title": title, "channel": channel, "format": fmt, "status": status,
                            "created": now.isoformat(timespec="seconds")}
    if conversation_id:
        meta["conversation"] = conversation_id
    meta["about"] = ABOUT
    body = "\n\n".join(f"{_head(i, _part_title(t))}\n\n{x.strip()}" for i, (t, x) in enumerate(parts or [], start=1))
    try:
        root.mkdir(parents=True, exist_ok=True)
        for _ in range(5):  # two writers racing for one name: exactly one wins, the other takes the next name
            stem = _new_id(root, title, now)
            path = root / f"{stem}.md"
            if create_text_atomic(path, _join(meta, body)):
                return Draft(stem, path, meta, body, True)
    except OSError as e:
        raise ApiError(507, "draft_not_saved", f"The draft could not be saved in {root} ({e}).") from e
    raise ApiError(409, "draft_not_saved", f"The draft could not be given a free name in {root}.")


def append_part(paths: Paths, ref: Any, text: str, part_title: str = "") -> tuple[Draft, int, bool]:
    """Add one part at the end of the draft as it is on disk now. Returns (draft, part number, added); the same
    text as the last part is not added twice (a repeated call after a lost answer)."""
    path = resolve(paths, ref)
    head, clean = _clean_part(text)
    title = _part_title(part_title or head)
    if not clean:
        raise ApiError(422, "empty_part", "That part has no story text. Send the text of one part.")
    if len(clean) > PART_MAX_CHARS:
        raise ApiError(422, "part_too_long", f"One part can be at most {PART_MAX_CHARS} characters (about "
                                             f"{PART_WORDS_HINT} words); this one has {len(clean)}. Split it into two parts.")
    result: dict[str, Any] = {}

    def change(d: Draft) -> tuple[dict[str, Any], str] | None:
        parts = d.parts()
        if parts and " ".join(parts[-1][1].split()) == " ".join(clean.split()):
            result.update(n=len(parts), added=False)
            return None
        if len(parts) >= MAX_PARTS:
            raise ApiError(422, "too_many_parts", f"The draft {path} has {len(parts)} parts, the most a draft holds.")
        if len(d.narration()[0]) + len(clean) > SCRIPT_MAX_CHARS:
            raise ApiError(422, "draft_full", f"The draft {path} would be longer than a script can be "
                                              f"({SCRIPT_MAX_CHARS} characters). Start the video from it as it is.")
        n = len(PART_HEAD.findall(d.body)) + 1
        block = f"{_head(n, title)}\n\n{clean}"
        meta = dict(d.meta)
        if d.meta_ok and str(meta.get("status") or "") in ("started", "refused"):
            meta["status"] = "writing"  # more story after a start or a refusal: it is being written again
        result.update(n=len(parts) + 1, added=True)
        body = f"{d.body.rstrip()}\n\n{block}" if d.body.strip() else block
        return meta, body

    d = _write(path, change)
    return d, int(result["n"]), bool(result["added"])


def set_meta(paths: Paths, ref: Any, **fields: Any) -> Draft:
    """Change front-matter fields of a draft (its status after a start). Never touches the story text."""
    path = resolve(paths, ref)

    def change(d: Draft) -> tuple[dict[str, Any], str] | None:
        if not d.meta_ok:
            return None  # the owner's broken block is his to fix; the story text is what matters
        return {**d.meta, **fields}, d.body

    return _write(path, change)


def _scan(paths: Paths) -> list[Path]:
    root = drafts_dir(paths)
    try:
        files = [p for p in root.glob("*.md") if p.is_file()]
    except OSError:
        return []

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:  # removed while we looked
            return 0.0

    files.sort(key=mtime, reverse=True)
    return files[:SCAN_LIMIT]


def listing(paths: Paths, limit: int = 10) -> list[dict[str, Any]]:
    """The newest drafts, without their parts."""
    out: list[dict[str, Any]] = []
    for p in _scan(paths)[:limit]:
        try:
            c = card(_read(p))
        except OSError:
            continue
        c["parts"] = len(c["parts"])
        out.append(c)
    return out


def of_conversation(paths: Paths, cid: str) -> Draft | None:
    """The newest draft written in conversation `cid`, or None."""
    if not cid:
        return None
    for p in _scan(paths):
        try:
            d = _read(p)
        except OSError:
            continue
        if str(d.meta.get("conversation") or "") == cid:
            return d
    return None


def cut_note(paths: Paths, cid: str) -> str | None:
    """What the owner hears when a reply was cut by the cap while a draft of this conversation exists: the draft
    is intact and named. None when the conversation has no draft (the caller says its own note)."""
    try:
        d = of_conversation(paths, cid)
    except Exception:  # noqa: BLE001 - the note must never break the turn; the caller's note still goes out
        return None
    if d is None:
        return None
    c = card(d)
    n = len(c["parts"])
    return (f"My reply was cut off because it was too long for one reply, so the part I was writing was not saved. "
            f"Your draft is safe and unchanged: {c['path']} ({n} part{'' if n == 1 else 's'}, {c['words']} words). "
            "Nothing was started. Ask me to write the next part shorter, or open the file and edit it yourself.")


def _same_text(d: Draft, text: str) -> bool:
    return " ".join(d.narration()[0].split()) == " ".join(script_text(text)[0].split())


def keep_text(paths: Paths, *, text: str, title: str, channel: str, fmt: str, conversation_id: str = "") -> Draft:
    """Keep a whole story text the Assistant wrote as a draft (a refused `start_script_job`), split into parts at
    its own heading lines; the same text already in a draft is not saved twice."""
    for p in _scan(paths):
        try:
            d = _read(p)
        except OSError:
            continue
        if _same_text(d, text):
            return d
    parts: list[tuple[str, str]] = []
    title_now, chunk = "", []
    for line in text.replace("\r\n", "\n").split("\n"):
        if heading_line(line):
            if "\n".join(chunk).strip():
                parts.append((title_now, "\n".join(chunk).strip()))
            title_now, chunk = line.strip().lstrip("#").strip().strip("*_").strip(), []
        else:
            chunk.append(line)
    if "\n".join(chunk).strip():
        parts.append((title_now, "\n".join(chunk).strip()))
    if not title:
        words = script_text(text)[0].split()[:8]
        title = " ".join(words).rstrip(".,;:!?") or "Untitled story"
    return create(paths, title=title, channel=channel, fmt=fmt, conversation_id=conversation_id, parts=parts,
                  status="refused")


# --------------------------------------------------------------------------- open (the card's button)


def _notepad(path: Path) -> None:
    """Open `path` in Notepad (every Windows has it) when no program is set for `.md` files."""
    starter = getattr(os, "startfile", None)
    if starter is None:
        raise OSError("opening files needs Windows")
    starter("notepad.exe", "open", f'"{path}"')


def open_draft(paths: Paths, opener: Any, ref: Any, notepad: Callable[[Path], None] | None = None) -> dict[str, Any]:
    """Open a draft with the system's default program for `.md` (Notepad when there is none).

    Only a `.md` file directly inside `<home>/drafts` is opened (`resolve`); anything else is refused before any
    program starts.
    """
    path = resolve(paths, ref)
    try:
        opener.open(path)
    except AdapterUnavailable as e:
        raise ApiError(501, "open_unavailable", f"{e}. The draft is at {path}.") from e
    except OSError:
        if os.name != "nt":
            raise ApiError(501, "open_unavailable", f"No program can open it here. The draft is at {path}.") from None
        try:
            (notepad or _notepad)(path)
        except OSError as e:
            raise ApiError(501, "open_unavailable", f"No program opened it ({e}). The draft is at {path}.") from e
    return {"ok": True, "opened": str(path)}


# --------------------------------------------------------------------------- the tools' bodies


def _cid(c: ToolContext) -> str:
    return str(getattr(c, "conversation_id", "") or "")


def tool_create(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """draft_create: a new draft for a channel (the channel must exist), optionally with its first part."""
    slug = str(a.get("channel") or "")
    ch = c.dash.channels.load(slug)
    fmt = str(a.get("format") or ch.format)
    if fmt not in FORMATS:
        fmt = "master_first"
    parts: list[tuple[str, str]] = []
    if str(a.get("text") or "").strip():
        head, clean = _clean_part(str(a["text"]))
        if len(clean) > PART_MAX_CHARS:
            raise ApiError(422, "part_too_long", f"One part can be at most {PART_MAX_CHARS} characters (about "
                                                 f"{PART_WORDS_HINT} words). Create the draft, then add the parts one by one.")
        if clean:
            parts.append((" ".join(str(a.get("part_title") or head).split())[:100], clean))
    d = create(c.dash.state.paths, title=str(a.get("title") or ""), channel=slug, fmt=fmt,
               conversation_id=_cid(c), parts=parts)
    return {"draft": card(d), "note": f"Draft saved at {d.path}. Add the story one part per call with draft_append_part."}


def tool_append(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """draft_append_part: one part at the end of the file as it is now."""
    d, n, added = append_part(c.dash.state.paths, a.get("draft"), str(a.get("text") or ""), str(a.get("part_title") or ""))
    info = card(d)
    note = (f"Part {n} saved. The draft has {len(info['parts'])} parts and {info['words']} words: {d.path}." if added
            else f"Part {n} already has this text; nothing was added. The draft has {info['words']} words: {d.path}.")
    return {"draft": info, "part": n, "added": added, "note": note}


def tool_read(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """draft_read: the outline and the end of the story (or one part's text)."""
    d = load(c.dash.state.paths, a.get("draft"))
    info = card(d)
    parts = d.parts()
    out: dict[str, Any] = {"draft": info}
    n = a.get("part")
    if isinstance(n, int) and not isinstance(n, bool):
        if not 1 <= n <= len(parts):
            raise ApiError(422, "no_such_part", f"The draft {d.path} has {len(parts)} parts, not a part {n}.")
        out["part"] = n
        out["text"] = parts[n - 1][1]
    elif parts:
        tail = script_text(parts[-1][1])[0].split()
        out["ends_with"] = ("... " if len(tail) > 80 else "") + " ".join(tail[-80:])
    return out


def tool_list(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """list_drafts: the newest drafts."""
    return {"folder": str(drafts_dir(c.dash.state.paths)), "drafts": listing(c.dash.state.paths, int(a.get("limit", 10)))}


def _job_body(c: ToolContext, a: dict[str, Any]) -> tuple[dict[str, Any], Draft, list[str]]:
    d = load(c.dash.state.paths, a.get("draft"))
    text, left_out = d.narration()
    if not text:
        raise ApiError(422, "draft_empty", f"The draft {d.path} has no story text yet.")
    body = {
        "channel": str(a.get("channel") or d.meta.get("channel") or ""),
        "format": a.get("format") or (d.meta.get("format") if d.meta.get("format") in FORMATS else None),
        "script": text,
        "script_title": str(a.get("title") or d.meta.get("title") or "").strip()[:60] or None,
        "count": 1,
    }
    return body, d, left_out


def _named(e: ApiError, d: Draft) -> ApiError:
    """The refusal with the draft named: the owner is pointed at his file, never only at an error (G-47)."""
    words = card(d)["words"]
    msg = (f"{e.message.rstrip()} The draft is kept as it is: {d.path} ({words} words). Edit it or ask me to add "
           "to it, then start it again.")
    return ApiError(e.status, e.code, msg, hint=e.hint, details=e.details)


def check_start(c: ToolContext, a: dict[str, Any]) -> None:
    """start_job_from_draft's dry run: the same checks `POST /api/jobs/run` makes, on the file as it is now."""
    body, d, _ = _job_body(c, a)
    try:
        c.dash.jobs.check_run(body)
    except ApiError as e:
        raise _named(e, d) from e


def tool_start(c: ToolContext, a: dict[str, Any]) -> dict[str, Any]:
    """start_job_from_draft after the owner's Yes: the file is read NOW, so his last edits are what is narrated."""
    body, d, left_out = _job_body(c, a)
    try:
        job = c.dash.jobs.create_run(body)
    except ApiError as e:
        raise _named(e, d) from e
    job_id = str(job.get("job_id") or "")
    words = word_count(body["script"])
    note = f"Started from {d.path} ({words} words). Changes to the file now do not change this video."
    try:  # the job exists already: a status that cannot be written must not turn it into a failure
        set_meta(c.dash.state.paths, d.path.stem, status="started", job_id=job_id,
                 started=datetime.now().astimezone().isoformat(timespec="seconds"))
    except ApiError:
        note += " (The draft's status line could not be updated.)"
    out: dict[str, Any] = {**job, "path": str(d.path), "draft_id": d.draft_id, "words": words, "note": note}
    if left_out:
        out["left_out"] = left_out[:20]
    return out
