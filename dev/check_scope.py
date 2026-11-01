"""C10: pull-request scope guard.

A branch named ``feat/c<K>-<slug>`` may change only the paths owned by component C<K>
(``docs/ENGINEERING.md`` section 2.1), plus a few shared places every builder may touch
(``tests/`` except ``tests/conftest.py``, ``contracts/CHANGE_REQUESTS.md``, its own plan file).
``ns_common/`` and ``contracts/`` may be changed only from ``feat/c3-*``, ``feat/release-*`` or
``orchestrator/*`` branches.

Usage (CI, pull requests only)::

    python dev/check_scope.py --base origin/main --branch feat/c4-tts

Exit codes: 0 clean (or nothing to check), 1 violations, 2 usage or git error. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

#: Branch prefixes that may edit everything (the main session / component C3).
# feat/release-* is the release-readiness builder (packaging, pins, contracts applied for the release).
ORCHESTRATOR_PREFIXES = ("feat/c3-", "feat/release-", "orchestrator/")

#: Paths nobody but the orchestrator edits, as prefixes (ending in "/") or exact paths.
PROTECTED = ("ns_common/", "contracts/", "manifest.json", "uv.lock", "tests/conftest.py", "pyproject.toml")

PROTECTED_REASON = "protected path: only feat/c3-*, feat/release-* or orchestrator/* branches may change it"

#: Protected paths that remain open to everyone.
PROTECTED_EXCEPTIONS = ("contracts/CHANGE_REQUESTS.md",)

#: Protected paths that a specific component may also edit.
PROTECTED_EXTRA_OWNERS: dict[str, tuple[int, ...]] = {"pyproject.toml": (10,)}

#: Component id -> owned paths (prefix when ending in "/", exact path otherwise).
OWNERSHIP: dict[int, tuple[str, ...]] = {
    1: ("INSTALL.cmd", "UNINSTALL.cmd", "installer/", "launchers/", "OPERATOR.md"),
    2: ("ns_supervisor/", "opencode/"),
    3: ("ns_common/", "contracts/", "manifest.json", "tools/", "uv.lock"),
    4: ("ns_tts/",),
    5: ("ns_studio/text/", "ns_studio/prompts/", "ns_studio/exemplars/", "ns_studio/calibrate/"),
    6: ("ns_studio/media/", "ns_studio/presets/"),
    7: (
        "ns_studio/__init__.py",
        "ns_studio/context.py",
        "ns_studio/stages.py",
        "ns_studio/orchestrator.py",
        "ns_studio/cli.py",
        "ns_studio/channels/",
        "ns_studio/variation/",
        "ns_studio/policy/",
        "ns_studio/learn/",
    ),
    8: ("ns_studio/clipper/", "ns_studio/presets/clip_templates.yaml"),
    9: ("ns_dashboard/",),
    10: (
        ".github/",
        "dev/",
        "CHANGELOG.md",
        "VERSION",
        ".gitattributes",
        ".gitignore",
        "pyproject.toml",
        "docs/REPO_SETTINGS.md",
    ),
}

#: Paths carved out of a component's owned prefixes.
EXCLUSIONS: dict[int, tuple[str, ...]] = {6: ("ns_studio/presets/clip_templates.yaml",)}

BRANCH_RE = re.compile(r"^feat/c(\d+)-")


@dataclass(frozen=True)
class Violation:
    """One changed path that the branch is not allowed to touch."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def _matches(path: str, pattern: str) -> bool:
    """True when ``path`` is ``pattern`` (exact file) or lies under it (pattern ending in ``/``)."""
    return path.startswith(pattern) if pattern.endswith("/") else path == pattern


def _any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_matches(path, p) for p in patterns)


def component_of(branch: str) -> int | None:
    """Return K for ``feat/c<K>-...`` branch names, else None."""
    m = BRANCH_RE.match(branch)
    return int(m.group(1)) if m else None


def is_orchestrator(branch: str) -> bool:
    """True for branches that may edit ``ns_common/`` and ``contracts/``."""
    return branch.startswith(ORCHESTRATOR_PREFIXES)


def _is_protected(path: str, comp: int | None) -> bool:
    if _any(path, PROTECTED_EXCEPTIONS):
        return False
    if not _any(path, PROTECTED):
        return False
    owners = tuple(PROTECTED_EXTRA_OWNERS.get(path, ()))
    return comp not in owners


def _own_plan(path: str, comp: int) -> bool:
    return re.fullmatch(rf"docs/superpowers/plans/m\d+-c{comp}-[^/]+\.md", path) is not None


def _shared_ok(path: str, comp: int) -> bool:
    if _any(path, PROTECTED_EXCEPTIONS) or _own_plan(path, comp):
        return True
    return path.startswith("tests/") and path != "tests/conftest.py"


def check(branch: str, files: list[str]) -> list[Violation]:
    """Return the violations for ``files`` changed on ``branch`` (empty list means clean)."""
    if is_orchestrator(branch) or branch in ("main", "master"):
        return []
    comp = component_of(branch)
    out: list[Violation] = []
    for path in sorted(set(files)):
        if _is_protected(path, comp):
            out.append(Violation(path, PROTECTED_REASON))
            continue
        if comp is None:
            continue  # unknown branch naming: only the protected-path rule applies
        if comp not in OWNERSHIP:
            out.append(Violation(path, f"branch names unknown component C{comp}"))
            continue
        if _shared_ok(path, comp):
            continue
        owned = _any(path, OWNERSHIP[comp]) and not _any(path, EXCLUSIONS.get(comp, ()))
        if not owned:
            out.append(Violation(path, f"outside the paths owned by C{comp}"))
    return out


def changed_files(base: str) -> list[str]:
    """Files changed between the merge base of ``base`` and HEAD (renames reported as delete + add)."""
    proc = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--no-renames", f"{base}...HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff against {base} failed: {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def _current_branch() -> str:
    env = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
    if env:
        return env
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, encoding="utf-8"
    )
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; see the module docstring."""
    ap = argparse.ArgumentParser(description="Check that a branch stays inside its component's paths.")
    ap.add_argument("--base", default="origin/main", help="base ref to diff against (default origin/main)")
    ap.add_argument("--branch", default="", help="branch name (default: GITHUB_HEAD_REF or current branch)")
    ap.add_argument("--files", nargs="*", help="check this explicit file list instead of running git")
    args = ap.parse_args(argv)

    branch = args.branch or _current_branch()
    try:
        files = list(args.files) if args.files is not None else changed_files(args.base)
    except RuntimeError as e:
        print(f"scope-check: {e}", file=sys.stderr)
        return 2
    comp = component_of(branch)
    label = f"C{comp}" if comp is not None else ("orchestrator" if is_orchestrator(branch) else "unscoped")
    violations = check(branch, files)
    print(f"scope-check: branch={branch} ({label}) files={len(files)} violations={len(violations)}")
    for v in violations:
        print(f"  VIOLATION {v}")
    if violations:
        print("Move the change to the owning component, or file a request in contracts/CHANGE_REQUESTS.md.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
