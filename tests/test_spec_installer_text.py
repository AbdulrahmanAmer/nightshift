"""P37: the installer text of BUILD_SPEC.md (A.2, A.3, A.5, 1.2, 1.3, 5, 21.1, 21.5, 21.6, J1, 13) states
what the installer code does, and the section 13 rows record the operator decisions of 2026-11-09.

Each test reads the spec line and the code it describes, so a change to either side that makes them disagree
fails here. Boxes: F-H1 F-H2 F-H3 F-H4 F-H7 F-H9 F-H10 F-H26 F-H27 F-H28 of docs/FEATURE_STATUS.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from installer.stage2 import sysinfo
from installer.stage2.steps import bridge_steps, default_steps
from installer.stage2.steps.s00_welcome import S00Welcome
from installer.stage2.steps.s01_questions import NO_KEY_NOTICE
from installer.stage2.web.bootstrap import TOOLS

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "BUILD_SPEC.md").read_text(encoding="utf-8")
STATUS = (ROOT / "docs" / "FEATURE_STATUS.md").read_text(encoding="utf-8")
STAGE1 = (ROOT / "installer" / "stage1.ps1").read_text(encoding="utf-8")
WELCOME = (ROOT / "installer" / "welcome.txt").read_text(encoding="utf-8")


def section(prefix: str) -> str:
    """The spec section whose heading starts with ``prefix``, up to the next heading of its level."""
    lines = SPEC.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(prefix))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    out = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.startswith("#") and len(ln) - len(ln.lstrip("#")) <= level:
            break
        out.append(ln)
    return "\n".join(out)


def table_row(text: str, first_cell: str) -> str:
    """The table line of ``text`` whose first cell starts with ``first_cell``."""
    return next(ln for ln in text.splitlines() if ln.startswith(f"| {first_cell}"))


def numbered_line(text: str, number: int) -> str:
    """The line ``<number>. ...`` of a numbered list."""
    return next(ln for ln in text.splitlines() if ln.startswith(f"{number}. "))


def decision(marker: str) -> str:
    """The section 13 row whose question cell contains ``marker`` (the test fails when there is none)."""
    rows = [ln for ln in section("## 13. Decisions").splitlines() if re.match(r"^\| D\d+ \|", ln)]
    hits = [ln for ln in rows if marker in ln.split("|")[2]]
    assert len(hits) == 1, f"section 13 needs exactly one row about {marker!r}, found {len(hits)}"
    return hits[0]


def decision_id(marker: str) -> str:
    return decision(marker).split("|")[1].strip()


def box(box_id: str) -> str:
    """The section H line of a box of docs/FEATURE_STATUS.md."""
    return next(
        ln
        for ln in STATUS.splitlines()
        if ln.startswith(f"- [ ] {box_id} ") or ln.startswith(f"- [x] {box_id} ")
    )


def status_row(feature_start: str) -> list[str]:
    """The cells of the audit row whose feature cell starts with ``feature_start``."""
    statuses = {"built", "partial", "missing", "spec_dropped"}
    for ln in STATUS.splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) >= 7 and cells[0] in statuses and cells[1].startswith(feature_start):
            return cells
    raise AssertionError(f"no audit row starts with {feature_start!r}")


def ps1_int(name: str) -> int:
    m = re.search(rf"\$script:{name} = (\d+)", STAGE1)
    assert m, name
    return int(m.group(1))


# ---------------------------------------------------------------------------------------------------------
# F-H2: free disk. F-H7: where the check blocks
# ---------------------------------------------------------------------------------------------------------
def test_welcome_names_the_computed_disk_amount_not_40_gb():
    """A.2 and welcome.txt give the first-install figure setup computes (H-6), not ~40 GB."""
    small = sysinfo.required_free_gb("small")
    assert small == 60
    a2 = section("### A.2")
    for text in (a2, WELCOME):
        assert "~40 GB" not in text
        assert f"{small} GB" in text and "exact number" in text


def test_disk_row_says_where_it_blocks():
    """1.2: Checks, Choices and s01 block under the one computed number; stage 1 only a drive with no room for
    the setup tools and it ranks drives with the same figure."""
    assert ps1_int("MinFreeGbHard") == 2 and ps1_int("RoomyGb") == sysinfo.required_free_gb("small")
    row = table_row(section("### 1.2"), "Disk |")
    assert "Choices" in row and "NS-E104" in row and "diskneed" in row
    for tier in ("small", "balanced", "best"):
        assert f"{sysinfo.required_free_gb(tier)} GB" in row


def test_disk_decision_row():
    row = decision("Free disk space")
    for needle in ("60 GB", "Balanced 64", "Best 72", "diskneed", "Choices", "NS-E104"):
        assert needle in row


# ---------------------------------------------------------------------------------------------------------
# F-H3: stock-footage keys are optional
# ---------------------------------------------------------------------------------------------------------
def test_keys_are_optional_everywhere():
    """A.2, welcome.txt, A.5, 5 s01 and 21.6 say optional, at least one recommended (setup goes on)."""
    assert "Setup continues" in NO_KEY_NOTICE
    assert "two free stock-footage API keys" not in section("### A.2")
    assert "two free stock-footage API keys" not in WELCOME
    for text in (section("### A.2"), WELCOME):
        assert "optional" in text.lower() and "stock-footage" in text
    a5 = table_row(section("### A.5"), "Pexels + Pixabay")
    assert a5.rstrip("| ").endswith("No") and "at least one" in a5
    assert "optional" in section("### 21.6").lower()
    assert "optional" in table_row(section("## 5. Installer"), "`s01_questions`").lower()


def test_keys_decision_row():
    row = decision("Stock-footage keys")
    for needle in ("optional", "at least one", "NS-E502", "Pexels"):
        assert needle in row


# ---------------------------------------------------------------------------------------------------------
# F-H4, F-H27: Git is not installed by setup
# ---------------------------------------------------------------------------------------------------------
def test_git_is_not_among_the_tools_setup_installs():
    """TOOLS has no Git; A.3 step 2, the Stage 1 line, 21.1 and J1 step 3 do not list it as installed."""
    assert all(
        "git" not in key.lower() and "git" not in winget.lower() for key, winget, _label, _req in TOOLS
    )
    for line in (numbered_line(section("### A.3"), 2), numbered_line(section("### J1"), 3)):
        assert "Git" not in line or re.search(
            r"Git (is|are) not (needed|installed)|does not install Git", line
        )
    assert "winget installs (Git" not in section("## 5. Installer")
    assert not any(ln.startswith("| Git |") for ln in section("### 21.1").splitlines())


def test_tools_table_lists_what_setup_installs():
    """Every tool of bootstrap.TOOLS has a row in 21.1 (FFmpeg, the Visual C++ runtime, Windows Terminal)."""
    s211 = section("### 21.1")
    for needle in (
        "Gyan.FFmpeg",
        "Microsoft.WindowsTerminal",
        "Microsoft.VCRedist.2015+.x64",
        "bins.ffmpeg",
        "bins.vcredist",
    ):
        assert needle in s211
    assert {key for key, *_ in TOOLS} == {"vcruntime", "ffmpeg", "terminal"}


def test_git_decision_row():
    row = decision("Git")
    for needle in ("ns update --git", "NS-E702", "not installed"):
        assert needle in row


def test_update_git_needs_git_and_says_so():
    """Section 5 says `ns update --git` needs Git, which setup does not install, and what it does without."""
    from ns_supervisor import update

    assert "Git is not installed" in update.NO_GIT_HINT
    para = next(ln for ln in section("## 5. Installer").splitlines() if ln.startswith("**`ns update`**"))
    assert "does not install" in para and "NS-E702" in para


# ---------------------------------------------------------------------------------------------------------
# F-H10, F-H28, F-H9: winget, direct downloads, unreachable sites
# ---------------------------------------------------------------------------------------------------------
def test_winget_row_is_a_warning_with_pinned_downloads():
    """1.2 and 21.1 say 'pinned direct downloads', drop NS-E111 and the Store page; stage1 never fails it."""
    row = table_row(section("### 1.2"), "winget |")
    assert "pinned" in row and "NS-E111" not in row and "Store" not in row
    s211 = section("### 21.1")
    assert "pinned direct download" in s211 and "NS-E111" not in s211 and "Microsoft Store" not in s211
    assert not re.search(r"New-Row 'winget'[^\n]*'fail'", STAGE1)
    assert not re.search(r"New-Row 'winget'[^\n]*NS-E111", STAGE1)


def test_winget_decision_row():
    row = decision("winget")
    for needle in ("warning", "pinned direct downloads", "bins.ffmpeg", "bins.vcredist", "NS-E111"):
        assert needle in row


def test_network_row_says_which_sites_block():
    """1.2: the three critical hosts are red, every other probed site is amber (both NS-E105)."""
    critical = re.search(r"\$script:CriticalHosts = @\(([^)]*)\)", STAGE1)
    assert critical
    hosts = re.findall(r"'([^']+)'", critical.group(1))
    assert hosts == ["github.com", "pypi.org", "files.pythonhosted.org"]
    row = table_row(section("### 1.2"), "Network |")
    for host in hosts:
        assert host in row
    for site in ("huggingface.co", "download.pytorch.org", "api.pexels.com", "pixabay.com"):
        assert site in STAGE1 and site in row
    assert "warning" in row.lower() and "NS-E105" in row


def test_network_decision_row():
    row = decision("Network")
    for needle in ("github.com", "huggingface.co", "NS-E105", "models_source"):
        assert needle in row


# ---------------------------------------------------------------------------------------------------------
# F-H1, F-H26: the window shows a short welcome; the console path prints welcome.txt
# ---------------------------------------------------------------------------------------------------------
def test_window_has_its_own_welcome_and_the_console_prints_welcome_txt():
    assert any(isinstance(s, S00Welcome) for s in default_steps())
    assert not any(isinstance(s, S00Welcome) for s in bridge_steps())
    assert "short welcome" in section("### A.2")
    assert "welcome.txt" in section("### A.2")
    step2 = numbered_line(section("### J1"), 2)
    assert "setup window" in step2 and "short welcome" in step2
    assert "A console opens" not in step2


def test_welcome_decision_row():
    row = decision("Welcome")
    for needle in ("setup window", "welcome.txt", "short"):
        assert needle in row


# ---------------------------------------------------------------------------------------------------------
# default home and the CUDA rule
# ---------------------------------------------------------------------------------------------------------
def test_default_home_is_the_drive_with_most_free_space_everywhere():
    """1.3 and 15.1 no longer say %LOCALAPPDATA% is the default; 5 and 21.5 keep the drive rule."""
    home_line = next(ln for ln in section("### 1.3").splitlines() if ln.startswith("- **The partner**"))
    assert "(`%LOCALAPPDATA%\\Nightshift`)" not in home_line and "most free space" in home_line
    names = table_row(section("### 15.1"), "Home env var")
    assert "default `%LOCALAPPDATA%\\Nightshift`" not in names and "most free space" in names
    assert "most free space" in table_row(section("## 5. Installer"), "`s01_questions`")
    assert "largest free SSD" in section("### 21.5")
    assert "most free space" in decision("Default install folder")


def test_cuda_rule_names_the_builds():
    """1.2 and 21.1: CUDA 12.4+ takes the 12.4 build, 13.0+ the 13.4 build; RTX 50 needs 13.0."""
    assert sysinfo.llama_pair_for("12.4") == "cuda12" and sysinfo.llama_pair_for("12.9") == "cuda12"
    assert sysinfo.llama_pair_for("13.0") == "cuda13" and sysinfo.llama_pair_for("12.3") is None
    row = table_row(section("### 21.1"), "NVIDIA driver")
    for needle in ("12.4", "13.0", "RTX 50", "12.4 build", "13.4 build", "NS-E103"):
        assert needle in row
    row12 = table_row(section("### 1.2"), "Driver |")
    assert "12.4" in row12 and "13.0" in row12 and "NS-E103" in row12
    drow = decision("NVIDIA driver")
    for needle in ("12.4", "13.0", "NS-E103", "NS-W115"):
        assert needle in drow


# ---------------------------------------------------------------------------------------------------------
# section 13 numbering and the audit file
# ---------------------------------------------------------------------------------------------------------
def test_section_13_numbers_run_without_gaps():
    nums = [
        int(m.group(1))
        for ln in section("## 13. Decisions").splitlines()
        if (m := re.match(r"^\| D(\d+) \|", ln))
    ]
    assert nums == list(range(1, len(nums) + 1))
    assert len(nums) >= 30


HEAD_BOXES = {
    "F-H1": "Welcome",
    "F-H2": "Free disk space",
    "F-H3": "Stock-footage keys",
    "F-H4": "Git",
    "F-H7": "Free disk space",
    "F-H9": "Network",
    "F-H10": "winget",
    "F-H26": "Welcome",
    "F-H27": "Git",
    "F-H28": "winget",
}


@pytest.mark.parametrize("box_id", sorted(HEAD_BOXES))
def test_h_box_cites_its_section_13_row_and_is_ticked_only_with_evidence(box_id):
    """The box names the decision row. P37 never ticks a box; QA ticks it, only with `evidence:`."""
    line = box(box_id)
    assert line.startswith("- [ ] ") or (line.startswith("- [x] ") and " evidence: " in line)
    assert f"section 13 {decision_id(HEAD_BOXES[box_id])}" in line


FIXED_ROWS = {
    "Welcome screen in the setup window": "spec_dropped",
    "A.2 'You'll need ~40 GB free'": "built",
    "A.2 'two free stock-footage API keys": "spec_dropped",
    "A.3.2 Install Git": "spec_dropped",
    "1.2 Disk: block if below 60 GB": "spec_dropped",
    "1.2 Network: HF, GitHub": "spec_dropped",
    "1.2 winget missing": "spec_dropped",
    "J1 step 2": "built",
    "J1 step 3": "built",
    "21.1 System-tools table fallbacks": "spec_dropped",
}


@pytest.mark.parametrize("feature", sorted(FIXED_ROWS))
def test_audit_row_status_follows_the_spec_text(feature):
    cells = status_row(feature)
    assert cells[0] == FIXED_ROWS[feature]


# ---------------------------------------------------------------------------------------------------------
# P37 break-it round: sentences that said more than the code does
# ---------------------------------------------------------------------------------------------------------
def ps1_function(name: str) -> str:
    """The body of a PowerShell function of stage1.ps1 (up to the next top-level function)."""
    m = re.search(rf"^function {re.escape(name)} \{{.*?(?=^function |\Z)", STAGE1, re.S | re.M)
    assert m, name
    return m.group(0)


def stage1_line() -> str:
    return next(ln for ln in section("## 5. Installer").splitlines() if ln.startswith("**Stage 1"))


def test_stage_1_installs_the_runtime_without_winget_and_the_spec_says_so():
    """Stage 1 downloads the pinned runtime and runs it elevated; only the tools step tries winget."""
    assert "winget" not in ps1_function("Install-VcRuntime").lower()
    assert "Install-VcRuntime -Manifest" in STAGE1
    step2 = numbered_line(section("### A.3"), 2)
    vc_row = table_row(section("### 21.1"), "Visual C++ runtime")
    for text in (step2, stage1_line(), vc_row):
        assert "stage 1" in text.lower() and "pinned" in text
    assert "winget first, then a pinned direct download for the runtime and FFmpeg" not in step2
    assert "no winget" in stage1_line()
    assert "FFmpeg" in step2 and "winget" in step2


def test_no_key_notice_is_named_by_its_three_routes():
    """D24: the console prints NO_KEY_NOTICE, the Done screen has the reminder, a resume shows a notice."""
    from installer.stage2.web import controller

    src = (ROOT / "installer" / "stage2" / "web" / "controller.py").read_text(encoding="utf-8")
    # the window's notice is added only on a locked resume; the Done result carries the reminder
    assert re.search(
        r"if locked and not any\(self\._keys_stored\.values\(\)\):\s+"
        r"state\[\"notices\"\]\.append\(dict\(NO_KEY_NOTICE\)\)",
        src,
    )
    assert controller.NO_KEY_NOTICE["code"] == "no-stock-key"
    assert '"stockKeyMissing": not self._stock_key_stored()' in src
    done = (ROOT / "installer" / "ui" / "src" / "screens" / "Done.tsx").read_text(encoding="utf-8")
    assert "stockKeyMissing" in done and "keyReminder" in done
    row = decision("Stock-footage keys")
    for needle in ("`NO_KEY_NOTICE`", "`stockKeyMissing`", "Done screen", "resume", "no-stock-key"):
        assert needle in row
    assert 'the window\'s "no-stock-key" notice)' not in row  # that wording named the wrong route


def test_ns_update_git_hint_is_for_a_clone_without_git(tmp_path, monkeypatch):
    """A zip install has no .git, so `ns update --git` says it is not a clone; the Git hint is for a clone."""
    from ns_common.errors import NsError
    from ns_common.paths import Paths
    from ns_supervisor import update
    from tests.test_ns_update import Env, make_app

    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    zip_app = make_app(tmp_path / "zip")
    (zip_app / ".git" / "HEAD").unlink()
    (zip_app / ".git").rmdir()
    with pytest.raises(NsError) as no_clone:
        update.git_update(zip_app, Env().env(), paths=Paths(tmp_path / "ns1"), latest_tag=lambda: "v0.2.0")
    assert no_clone.value.code == "NS-E702" and "not a git clone" in no_clone.value.detail
    assert update.NO_GIT_HINT not in no_clone.value.detail
    clone = make_app(tmp_path / "clone")
    with pytest.raises(NsError) as no_git:
        update.git_update(clone, Env().env(), paths=Paths(tmp_path / "ns2"), latest_tag=lambda: "v0.2.0")
    assert no_git.value.detail == update.NO_GIT_HINT
    para = next(ln for ln in section("## 5. Installer").splitlines() if ln.startswith("**`ns update`**"))
    for text in (numbered_line(section("### A.3"), 2), para, decision("Git"), section("### 21.1")):
        assert "clone" in text and "NS-E702" in text
    assert "not a git clone" in decision("Git") and "not a git clone" in para


def test_operator_guide_agrees_with_the_welcome_text():
    """OPERATOR.md ships in the bundle: it names the tier disk amounts and the optional key, as A.2 does."""
    guide = (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
    assert "Two free keys" not in guide and "the two stock footage keys" not in guide
    assert "About 75 GB" not in guide
    small, balanced, best = (sysinfo.required_free_gb(t) for t in ("small", "balanced", "best"))
    need = guide.split("## What you need", 1)[1].split("## Install", 1)[0]
    assert f"{small} GB" in need and f"{balanced} GB" in need and f"{best} GB" in need
    assert "optional" in need.lower() and "Pixabay" in need


def test_default_home_rules_include_the_drive_size_and_last_resort():
    """D29 states Select-DefaultHome as it is: roomy drives first, SSD over HDD, a last resort."""
    body = ps1_function("Select-DefaultHome")
    assert "RoomyGb" in body and "'SSD'" in body
    assert "Join-Path (Get-DataDir) 'Home'" in STAGE1
    row = decision("Default install folder")
    assert "60 GB" in row and "SSD" in row and "LOCALAPPDATA" in row
    assert "Nightshift\\Home" in row


def test_headless_switches_are_all_named():
    """A.2 and D26: -NoUi, NS_NONINTERACTIVE=1 or an answers.json go headless; -Ui overrides the file."""
    headless = "$script:Headless = (($answersPath -and -not $Ui) -or [bool]$NoUi"
    assert headless + " -or ($env:NS_NONINTERACTIVE -eq '1'))" in STAGE1
    for text in (section("### A.2"), decision("Welcome")):
        assert "NS_NONINTERACTIVE" in text and "`-Ui`" in text


def test_disk_rows_say_a_resume_counts_what_setup_already_holds():
    """1.2 and D23: on a locked resume the amber row counts the space setup already holds (held_gb)."""
    import inspect

    from installer.stage2.web import preflight

    assert "held_gb" in inspect.signature(preflight.raw_disk_rows).parameters
    for text in (table_row(section("### 1.2"), "Disk |"), decision("Free disk space")):
        assert "resume" in text and "already holds" in text


def test_counts_table_matches_the_rows_it_counts():
    """The Counts table agrees with the audit rows for partial, missing and spec_dropped, per area."""
    statuses = ("partial", "missing", "spec_dropped")
    per_area: dict[str, dict[str, int]] = {}
    area = ""
    for ln in STATUS.splitlines():
        if ln.startswith("## "):
            area = ln
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if area and len(cells) >= 7 and cells[0] in statuses:
            per_area.setdefault(area, dict.fromkeys(statuses, 0))[cells[0]] += 1
    table = STATUS.split("## Counts", 1)[1].split("\n## ", 1)[0]
    table_rows = [
        [c.strip().strip("*") for c in ln.strip().strip("|").split("|")] for ln in table.splitlines()
    ]
    by_name = {r[0]: r for r in table_rows if len(r) == 6 and r[1].isdigit()}
    installer = next(r for name, r in by_name.items() if name.startswith("Installer"))
    mine = per_area["## Installer, install sources, uninstall, update (spec A, 5, 21, J1, J8)"]
    assert [int(installer[i]) for i in (2, 3, 4)] == [mine[s] for s in statuses]
    total = by_name["All"]
    assert [int(total[i]) for i in (2, 3, 4)] == [sum(c[s] for c in per_area.values()) for s in statuses]
