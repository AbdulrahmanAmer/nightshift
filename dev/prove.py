"""Proofs are code (H-13): one runner for every proof script under ``proofs/``.

    python dev/prove.py --list
    python dev/prove.py --home D:\\NightShift\\installs\\dev --box F-B7 [--box F-C2 ...]
    python dev/prove.py --home D:\\NightShift\\installs\\dev --proof f-b7-review-reject
    python dev/prove.py --home D:\\NightShift\\installs\\dev --all [--no-window]

A proof is ``proofs/<slug>.py`` (the folder is a package; every name that starts with ``_`` is skipped:
``_lib``, ``_fakes``, ``_fixtures``) with a module-level literal and a ``run`` function::

    PROOF = {"id": "f-b7-review-reject", "boxes": ["F-B7"], "install": "dev", "window": False,
             "needs": ["engine"], "timeout_s": 300, "fakes": ["footage"]}

    def run(ctx): ...      # returning is a pass; ctx.check / ProofFailed / any exception is a fail

``id``, ``boxes``, ``install`` ("dev" | "engine" | "any") and ``window`` are required; ``needs`` (``[]`` or
``["engine"]``), ``timeout_s`` and ``fakes`` (information only) are optional. A proof with a missing or wrong
field is reported (``--list`` shows it, ``--all`` counts it as a failure); it never crashes the runner. The
literal is read with ``ast`` and the module is imported as ``<package>.<stem>`` only when it runs.

What the runner does, and only that: reads version, kind and ports from the install; checks the laptop lock
for a laptop install; starts the engine with ``refs\\tools\\nsup.ps1`` when a proof needs it and it is not up,
and brings it down with ``nsproc.ps1 -Down`` only if it started it; gives each proof a ``ctx`` (see
``dev/prove_ctx.py``) and kills exactly the pids that proof spawned; writes
``<runs>/proofs/<version>/<box>.json`` for every box of every proof it ran.

Exit codes: 0 every proof passed or was skipped, 1 any proof failed, 2 usage or setup error (no install, lock
not held, unknown box, no engine tool).

Standard library only. Never global input, nothing on C:, one proof at a time.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dev.prove_ctx import (  # noqa: E402
    Ctx,
    InstallInfo,
    ProofFailed,
    ProofSkipped,
    SetupError,
    jsonable,
    now_iso,
    read_install,
)

DEFAULT_PROOFS = ROOT / "proofs"
DEFAULT_TOOLS = Path(r"D:\NightShift\refs\tools")
DEFAULT_LOCK = Path(r"D:\NightShift\refs\departments\LAPTOP.lock")
KINDS = ("dev", "engine", "any")
NEEDS = ("engine",)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
BOX_RE = re.compile(r"^[A-Za-z]+-[A-Za-z0-9]+$")
DEFAULT_TIMEOUT_S = 300
ENGINE_START_TIMEOUT_S = 300


# -------------------------------------------------- discovery
@dataclass
class Proof:
    path: Path
    stem: str
    id: str = ""
    boxes: list[str] = field(default_factory=list)
    install: str = ""
    window: bool = False
    needs: list[str] = field(default_factory=list)
    timeout_s: float = DEFAULT_TIMEOUT_S
    fakes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def label(self) -> str:
        return self.id or self.stem


def _declared(tree: ast.Module) -> tuple[Any, bool, str | None]:
    """The PROOF literal (or None), whether `run` is defined, and why the literal could not be read."""
    literal: Any = None
    why: str | None = "no module-level PROOF = {...}"
    has_run = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PROOF" for t in node.targets
        ):
            try:
                literal, why = ast.literal_eval(node.value), None
            except (ValueError, SyntaxError):
                literal, why = None, "PROOF is not a literal (use plain strings, lists, numbers and booleans)"
    return literal, has_run, why


def _str_list(value: Any, name: str, problems: list[str], *, required: bool) -> list[str]:
    if value is None:
        if required:
            problems.append(f"missing field {name}")
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        problems.append(f"{name} must be a list of non-empty strings")
        return []
    if required and not value:
        problems.append(f"{name} must not be empty")
    return list(value)


def load_declaration(path: Path) -> Proof:
    """Read one proof file's declaration without importing it; every defect goes to ``problems``."""
    proof = Proof(path=path, stem=path.stem)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        proof.problems.append(f"cannot read the file: {exc}")
        return proof
    literal, has_run, why = _declared(tree)
    if why:
        proof.problems.append(why)
    if not has_run:
        proof.problems.append("no `def run(ctx)` at module level")
    if not isinstance(literal, dict):
        if why is None:
            proof.problems.append("PROOF must be a dict")
        return proof
    p = proof.problems
    pid = literal.get("id")
    if pid is None:
        p.append("missing field id")
    elif not isinstance(pid, str) or not ID_RE.match(pid):
        p.append("id must be lower-case letters, digits, '-' or '_'")
    else:
        proof.id = pid
    proof.boxes = [b.upper() for b in _str_list(literal.get("boxes"), "boxes", p, required=True)]
    for b in proof.boxes:
        if not BOX_RE.match(b):
            p.append(f"box id {b!r} is not like F-B7")
    install = literal.get("install")
    if install is None:
        p.append("missing field install")
    elif install not in KINDS:
        p.append(f"install must be one of {', '.join(KINDS)}")
    else:
        proof.install = install
    window = literal.get("window")
    if window is None:
        p.append("missing field window")
    elif not isinstance(window, bool):
        p.append("window must be true or false")
    else:
        proof.window = window
    proof.needs = _str_list(literal.get("needs"), "needs", p, required=False)
    for n in proof.needs:
        if n not in NEEDS:
            p.append(f"needs may only name {', '.join(NEEDS)}, not {n!r}")
    proof.fakes = _str_list(literal.get("fakes"), "fakes", p, required=False)
    timeout = literal.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        p.append("timeout_s must be a positive number")
    else:
        proof.timeout_s = timeout
    unknown = sorted(set(literal) - {"id", "boxes", "install", "window", "needs", "timeout_s", "fakes"})
    if unknown:
        p.append(f"unknown field(s): {', '.join(unknown)}")
    return proof


def discover(proofs_dir: Path = DEFAULT_PROOFS) -> list[Proof]:
    """Every ``proofs/<name>.py`` whose name does not start with ``_`` (so ``_lib`` and friends are skipped).
    A repeated proof id marks the later file invalid."""
    found: list[Proof] = []
    if not Path(proofs_dir).is_dir():
        return found
    seen: dict[str, str] = {}
    for path in sorted(Path(proofs_dir).glob("*.py")):
        if path.name.startswith("_"):
            continue
        proof = load_declaration(path)
        if proof.id:
            if proof.id in seen:
                proof.problems.append(f"id {proof.id!r} is also declared by {seen[proof.id]}")
            else:
                seen[proof.id] = path.name
        found.append(proof)
    return found


def list_text(proofs: list[Proof]) -> str:
    rows = [("id", "boxes", "install", "window", "needs")]
    bad: list[str] = []
    for p in proofs:
        if p.valid:
            rows.append(
                (p.id, ",".join(p.boxes), p.install, "yes" if p.window else "no", ",".join(p.needs) or "-")
            )
        else:
            bad.append(f"INVALID {p.path.name}: " + "; ".join(p.problems))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    if len(rows) == 1:
        lines = ["no proofs found"]
    return "\n".join([*lines, *bad])


# -------------------------------------------------- the laptop lock
def _dept_name(text: str) -> str:
    return re.sub(r"^ns-", "", text.strip().lower())


def lock_holder(lock_path: Path) -> str | None:
    try:
        first = lock_path.read_text(encoding="utf-8-sig").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first.split("|")[0].strip() or None


def is_laptop_install(home: Path, lock_path: Path) -> bool:
    """A home under ``<NightShift root>\\installs`` (the root being three folders above the lock file)."""
    try:
        Path(os.path.abspath(home)).relative_to(Path(os.path.abspath(lock_path)).parents[2] / "installs")
    except (ValueError, IndexError):
        return False
    return True


def lock_problem(home: Path, lock_path: Path, dept: str) -> str | None:
    """Why this run must not start, or None. A laptop install needs the caller to hold LAPTOP.lock."""
    if not is_laptop_install(home, lock_path):
        return None
    holder = lock_holder(lock_path)
    if holder is None:
        return f"{home} is a laptop install: take {lock_path} first (one line: <dept> | <what> | <time>)"
    if _dept_name(holder) != _dept_name(dept):
        return f"{lock_path} is held by '{holder}', not '{dept}': do not run; try later"
    return None


# -------------------------------------------------- results
@dataclass
class Result:
    proof: Proof
    result: str  # pass | fail | skipped
    reason: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    started: str = ""
    duration_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _default_ps(script: Path, args: list[str], timeout: float) -> tuple[int, str]:
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def _default_probe(info: InstallInfo) -> bool:
    import urllib.request

    url = f"http://127.0.0.1:{info.ports['dashboard']}/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def _git_commit() -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return done.stdout.strip() or "unknown"


class Runner:
    """Runs the chosen proofs one at a time against one home."""

    def __init__(
        self,
        home: Path,
        *,
        runs: Path,
        proofs_dir: Path = DEFAULT_PROOFS,
        tools: Path = DEFAULT_TOOLS,
        lock_path: Path = DEFAULT_LOCK,
        no_window: bool = False,
        commit: str | None = None,
        ps: Callable[[Path, list[str], float], tuple[int, str]] = _default_ps,
        probe: Callable[[InstallInfo], bool] = _default_probe,
        ctx_options: dict[str, Any] | None = None,
        out: Callable[[str], None] = print,
    ) -> None:
        self.home = Path(home)
        self.runs = Path(runs)
        self.proofs_dir = Path(proofs_dir)
        self.tools = Path(tools)
        self.lock_path = Path(lock_path)
        self.no_window = no_window
        self.commit = commit if commit is not None else _git_commit()
        self.info = read_install(self.home)
        self.ps, self.probe, self.out = ps, probe, out
        self.ctx_options = ctx_options or {}
        self.laptop = is_laptop_install(self.home, self.lock_path)
        self.evidence_dir = self.runs / "proofs" / self.info.version
        self.lifecycle: dict[str, Any] = {}

    # --------------------------------------------------------------------------------- engine lifecycle
    def _nsproc(self, *extra: str) -> tuple[int, str]:
        return self.ps(self.tools / "nsproc.ps1", ["-NsHome", str(self.home), *extra], 180)

    def _ensure_engine(self) -> str | None:
        """Start supervisor and dashboard when they are not up. Returns an error text, or None."""
        life = self.lifecycle
        if self.probe(self.info):
            life["engine"] = "already up (not started by the runner)"
            return None
        nsup = self.tools / "nsup.ps1"
        if not nsup.is_file():
            life["engine"] = "not up and not started"
            return f"engine not up and {nsup} not found"
        rc, text = self.ps(nsup, ["-NsHome", str(self.home)], ENGINE_START_TIMEOUT_S)
        life["started_by_runner"] = True  # brought down at the end even when the start failed half way
        life["nsup_exit"] = rc
        life["nsup_output"] = text[-1500:]
        _rc, procs = self._nsproc()
        life["pids_after_start"] = [int(m) for m in re.findall(r"^\s*(\d+)\s+\d+ MB", procs, re.M)]
        if rc != 0 or not self.probe(self.info):
            life["engine"] = "start failed"
            return f"engine did not start (nsup exit {rc}): {text[-300:]}"
        life["engine"] = "started by the runner"
        return None

    def _bring_down(self) -> None:
        life = self.lifecycle
        if life.get("started_by_runner"):
            rc, text = self._nsproc("-Down")
            life["down_exit"] = rc
            life["nsproc_after"] = text[-1500:]
        else:
            life["nsproc_after"] = self._nsproc()[1][-1500:]

    # --------------------------------------------------------------------------------- one proof
    def _import(self, proof: Proof) -> Any:
        importlib.invalidate_caches()
        module = importlib.import_module(f"{self.proofs_dir.name}.{proof.stem}")
        if not callable(getattr(module, "run", None)):
            raise ProofFailed(f"{proof.path.name} has no callable run(ctx)")
        return module

    def _run_one(self, proof: Proof, engine_error: str | None) -> Result:
        started, t0 = now_iso(), time.monotonic()
        scratch = self.evidence_dir / proof.id
        if scratch.exists():
            import shutil

            shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        if self.laptop:
            env["NS_LAPTOP_LOCK_MINE"] = "1"
        ctx = Ctx(
            home=self.home,
            info=self.info,
            scratch=scratch,
            proof_id=proof.id,
            timeout_s=proof.timeout_s,
            env=env,
            **self.ctx_options,
        )
        result, reason, tb = "pass", "", ""
        try:
            if "engine" in proof.needs and engine_error:
                raise ProofFailed(engine_error)
            self._import(proof).run(ctx)
        except ProofSkipped as skip:
            result, reason = "skipped", str(skip) or "skipped"
        except ProofFailed as exc:
            result, reason, tb = "fail", str(exc), traceback.format_exc()
        except Exception as exc:  # a crashing proof is a failed proof, never a crashed runner
            result, reason, tb = "fail", f"{type(exc).__name__}: {exc}", traceback.format_exc()
        finally:
            ctx.close()
        extra: dict[str, Any] = {
            "pids": ctx.pids,
            "left_on_screen": ctx.left_on_screen,
            "scratch": str(scratch),
        }
        if tb:
            extra["traceback"] = tb
        return Result(proof, result, reason, ctx.steps, started, round(time.monotonic() - t0, 2), extra)

    # --------------------------------------------------------------------------------- many proofs
    def execute(self, proofs: list[Proof], *, quiet_skips: bool = False) -> list[Result]:
        results: list[Result] = []
        runnable: list[Proof] = []
        for p in proofs:
            if not p.valid:
                results.append(
                    Result(p, "fail", "invalid PROOF declaration: " + "; ".join(p.problems), [], now_iso())
                )
            elif p.install != "any" and p.install != self.info.kind:
                if not quiet_skips:
                    results.append(Result(p, "skipped", "wrong install kind", [], now_iso()))
            elif p.window and self.no_window:
                results.append(Result(p, "skipped", "window proof skipped by --no-window", [], now_iso()))
            else:
                runnable.append(p)
        self.lifecycle = {"started_by_runner": False}
        if runnable:
            self.lifecycle["nsproc_before"] = self._nsproc()[1][-1500:]
        engine_error: str | None = None
        added = str(self.proofs_dir.parent)
        sys.path.insert(0, added)
        self._purge_modules()
        try:
            if any("engine" in p.needs for p in runnable):
                engine_error = self._ensure_engine()
            for p in runnable:
                self.out(f"running {p.id} ...")
                results.append(self._run_one(p, engine_error))
        finally:
            try:
                if runnable:
                    self._bring_down()
            finally:
                self._purge_modules()
                if added in sys.path:
                    sys.path.remove(added)
        self.write_evidence(results)
        return results

    def _purge_modules(self) -> None:
        pkg = self.proofs_dir.name
        for name in [n for n in sys.modules if n == pkg or n.startswith(pkg + ".")]:
            del sys.modules[name]

    # --------------------------------------------------------------------------------- evidence
    def write_evidence(self, results: list[Result]) -> list[Path]:
        written: list[Path] = []
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            if not r.proof.valid:
                names = [f"_invalid-{r.proof.stem}"]
            else:
                names = list(r.proof.boxes)
            for name in names:
                record = {
                    "proof_id": r.proof.label,
                    "box": name,
                    "result": r.result,
                    "reason": r.reason,
                    "steps": r.steps,
                    "version": self.info.version,
                    "kind": self.info.kind,
                    "home": str(self.home),
                    "NIGHTSHIFT_HOME": str(self.home),
                    "started": r.started,
                    "duration_s": r.duration_s,
                    "runner_commit": self.commit,
                    "lifecycle": jsonable(self.lifecycle),
                    **jsonable(r.extra),
                }
                target = self.evidence_dir / f"{name}.json"
                tmp = target.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, target)
                written.append(target)
        return written


# -------------------------------------------------- selection / CLI
def select(
    proofs: list[Proof], boxes: list[str], ids: list[str], everything: bool
) -> tuple[list[Proof], list[str]]:
    """The proofs a command line asks for, and the usage errors (an unknown box or proof)."""
    errors: list[str] = []
    chosen: list[Proof] = []

    def add(p: Proof) -> None:
        if p not in chosen:
            chosen.append(p)

    if everything:
        for p in proofs:
            add(p)
    for box in (b.upper() for b in boxes):
        hits = [p for p in proofs if box in p.boxes]
        if not hits:
            errors.append(f"no proof declares box {box}")
        for p in hits:
            add(p)
    for pid in ids:
        hits = [p for p in proofs if pid in (p.id, p.stem)]
        if not hits:
            errors.append(f"no proof with id {pid!r}")
        for p in hits:
            add(p)
    return chosen, errors


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="prove.py", description="Run the proof scripts under proofs/ against an install."
    )
    ap.add_argument("--home", type=Path, help="the install (NIGHTSHIFT_HOME) to prove against")
    ap.add_argument(
        "--box", action="extend", nargs="+", default=[], metavar="ID", help="run the proofs of these boxes"
    )
    ap.add_argument(
        "--proof", action="extend", nargs="+", default=[], metavar="ID", help="run these proof ids"
    )
    ap.add_argument(
        "--all", action="store_true", dest="everything", help="run every proof the install kind fits"
    )
    ap.add_argument("--list", action="store_true", help="print id, boxes, install, window, needs and exit")
    ap.add_argument("--no-window", action="store_true", help="skip proofs that drive a page or a window")
    ap.add_argument("--runs", type=Path, help="evidence root (default: <home>\\..\\..\\runs)")
    ap.add_argument("--proofs-dir", type=Path, default=DEFAULT_PROOFS, help=argparse.SUPPRESS)
    ap.add_argument("--tools", type=Path, default=Path(os.environ.get("NS_TOOLS_DIR", DEFAULT_TOOLS)))
    ap.add_argument(
        "--lock-file", type=Path, default=Path(os.environ.get("NS_LAPTOP_LOCK_FILE", DEFAULT_LOCK))
    )
    ap.add_argument(
        "--dept", default=os.environ.get("NS_DEPT", "qa"), help="the department holding LAPTOP.lock"
    )
    return ap


def main(argv: list[str] | None = None, *, runner_options: dict[str, Any] | None = None) -> int:
    args = build_parser().parse_args(argv)
    proofs = discover(args.proofs_dir)
    if args.list:
        print(list_text(proofs))
        return 0
    if not (args.box or args.proof or args.everything):
        print("prove.py: give --list, or --box ID, --proof ID or --all (and --home)", file=sys.stderr)
        return 2
    if args.home is None:
        print("prove.py: --home <install> is required to run proofs", file=sys.stderr)
        return 2
    chosen, errors = select(proofs, args.box, args.proof, args.everything)
    if errors:
        print("prove.py: " + "; ".join(errors), file=sys.stderr)
        return 2
    if not chosen:
        print("prove.py: no proofs to run", file=sys.stderr)
        return 2
    if not args.home.is_dir():
        print(f"prove.py: {args.home} is not a folder", file=sys.stderr)
        return 2
    problem = lock_problem(args.home, args.lock_file, args.dept)
    if problem:
        print(f"prove.py: {problem}", file=sys.stderr)
        return 2
    runs = args.runs or Path(os.path.abspath(args.home)).parents[1] / "runs"
    try:
        runner = Runner(
            args.home,
            runs=runs,
            proofs_dir=args.proofs_dir,
            tools=args.tools,
            lock_path=args.lock_file,
            no_window=args.no_window,
            **(runner_options or {}),
        )
    except SetupError as exc:
        print(f"prove.py: {exc}", file=sys.stderr)
        return 2
    results = runner.execute(chosen, quiet_skips=args.everything and not (args.box or args.proof))
    for r in results:
        tail = f"  {r.reason}" if r.reason else ""
        print(f"{r.result.upper():7} {r.proof.label} [{','.join(r.proof.boxes)}] {r.duration_s}s{tail}")
    failed = sum(r.result == "fail" for r in results)
    print(
        f"{sum(r.result == 'pass' for r in results)} passed, {failed} failed, "
        f"{sum(r.result == 'skipped' for r in results)} skipped; evidence in {runner.evidence_dir}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
