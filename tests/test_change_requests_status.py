"""Status lines in contracts/CHANGE_REQUESTS.md that name a commit, a PR or a reader must be true."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "contracts" / "CHANGE_REQUESTS.md").read_text(encoding="utf-8")


def entry(title_start: str, occurrence: int = 0) -> str:
    """The text of one `## CR-...` entry, from its title line to the next entry."""
    starts = [m.start() for m in re.finditer(r"^## " + re.escape(title_start), TEXT, re.M)]
    assert len(starts) > occurrence, f"no entry {title_start!r} #{occurrence}"
    begin = starts[occurrence]
    nxt = TEXT.find("\n## ", begin + 1)
    return TEXT[begin : nxt if nxt != -1 else len(TEXT)]


def test_direct_commits_are_not_credited_to_a_pull_request() -> None:
    # 59e9a45 and 03a99dc were pushed straight to main on 2026-11-01; `gh api commits/<sha>/pulls` is empty.
    assert not re.search(r"59e9a45 \(PR", TEXT)
    assert not re.search(r"03a99dc, PR", TEXT)
    assert re.search(r"59e9a45 \(a direct commit on main", TEXT)
    assert re.search(r"03a99dc, a direct commit on main", TEXT)
    # The NS-E408 change that did come through a pull request is 95580e6 (PR 15).
    assert "95580e6 (PR 15)" in entry("CR-C6-3")


def test_dashboard_job_rejection_names_the_other_readers_and_writers() -> None:
    status = next(line for line in entry("CR-C9-2").splitlines() if line.startswith("- **Status:**"))
    assert "nothing outside `ns_dashboard` reads it" not in status
    assert "ns_supervisor/scheduled.py" in status
    assert "ns_studio/series/workshop.py" in status
    assert "make_db_enqueue" in status
    # The claims are true of the code the entry names.
    assert "schedule_at" in (ROOT / "ns_supervisor" / "scheduled.py").read_text(encoding="utf-8")
    workshop = (ROOT / "ns_studio" / "series" / "workshop.py").read_text(encoding="utf-8")
    assert "def make_db_enqueue" in workshop
    assert "insert or ignore into dashboard_jobs" in workshop


def test_dashboard_service_entry_lists_every_difference_from_the_request() -> None:
    from ns_dashboard import DEFAULT_PORT
    from ns_supervisor.services import DASHBOARD_PORT

    status = next(line for line in entry("CR-C9-3").splitlines() if line.startswith("- **Status:**"))
    assert "`/api/health`" in status
    assert "`persistent`" in status
    assert "no `--port 8100`" in status
    assert DEFAULT_PORT == DASHBOARD_PORT == 8100
    # `_dashboard` is the builder; the name in `SERVICES` is "dashboard".
    from ns_supervisor.services import SERVICES, _dashboard  # noqa: F401  (the symbol the entry names)

    assert "`SERVICES` has `dashboard`, built by `_dashboard`" in status
    assert "`_dashboard` is in `SERVICES`" not in status
    assert "dashboard" in SERVICES


def test_engineering_doc_does_not_promise_a_dashboard_job_schema() -> None:
    text = (ROOT / "docs" / "ENGINEERING.md").read_text(encoding="utf-8")
    assert "`clip_template`, `dashboard_job` if not already in v1" not in text


# ---- the reasons and the readers a Status line names must match the code ----


def status_of(title_start: str, occurrence: int = 0) -> str:
    """The `- **Status:**` line of one entry."""
    return next(
        line for line in entry(title_start, occurrence).splitlines() if line.startswith("- **Status:**")
    )


def source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def py_sources(*dirs: str) -> dict[str, str]:
    """Every `.py` file under the given top-level folders, by repo-relative path."""
    out: dict[str, str] = {}
    for d in dirs:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" not in p.parts:
                out[p.relative_to(ROOT).as_posix()] = p.read_text(encoding="utf-8")
    return out


RUNTIME_DIRS = ("installer", "ns_common", "ns_dashboard", "ns_studio", "ns_supervisor", "ns_tts")


def test_llama_build_reason_says_the_field_exists_but_nothing_sets_it() -> None:
    status = status_of("CR-C1-3")
    assert "already in `Runtime.llama.build`" not in status
    assert "`Runtime.llama.build` exists but nothing sets it" in status
    assert "not in the C3 amendment list" not in status and "per coordinator" not in status
    # The effect behind the sentence: the field is declared, defaults to null, and no code writes or reads it.
    assert 'build: Literal["cuda13", "cuda12"] | None = None' in source("ns_common/models.py")
    setter = re.compile(r"\.build\s*=(?!=)|LlamaCfg\([^)]*\bbuild\s*=|[\"']build[\"']\s*:")
    reader = re.compile(r"\bllama\.build\b|\bllama_cfg\.build\b")
    for rel, text in py_sources(*RUNTIME_DIRS).items():
        assert not setter.search(text), f"{rel} sets a build field: the CR-C1-3 reason is out of date"
        assert not reader.search(text), f"{rel} reads Runtime.llama.build: the CR-C1-3 reason is out of date"
        # An alias (`cfg = rt.llama`, `llama = load_runtime(p).llama`) must not read `.build` either, and no
        # attribute named `build` is read anywhere (`opts.build(...)` is a method call, which is allowed).
        for alias in re.findall(r"^\s*(\w+)\s*=\s*[^=\r\n]*\.llama\s*$", text, re.M):
            alias_read = rf"(?<!\w){alias}\.build(?!\w)|getattr\(\s*{alias}\s*,\s*[\"']build[\"']"
            assert not re.search(alias_read, text), (
                f"{rel} reads {alias}.build through an alias of Runtime.llama"
            )
        assert not re.search(r"\.build(?!\w)(?!\s*\()", text), f"{rel} reads a `.build` attribute"


def test_paths_claim_in_the_llama_build_entry_names_only_what_exists() -> None:
    status = status_of("CR-C1-3")
    assert "(plus `stories`, `assistant_db`)" not in status
    assert "removed again in 93cd8c8" in status
    from ns_common.paths import Paths

    for name in ("bin_uv", "bin_qwentts", "models_cv", "assistant_db"):
        assert isinstance(getattr(Paths, name, None), property), f"Paths.{name} is missing"
    assert not hasattr(Paths, "stories")


def test_wizard_flags_claims_in_the_llama_pair_rejection_hold() -> None:
    status = status_of("CR-C1-3")
    assert "nothing reads the `cuda` flag" in status
    assert "`ns_supervisor/doctor.py` only copies the file" in status
    s02 = source("installer/stage2/steps/s02_download_bins.py")
    assert 'ctx.set_flag("llama_pair"' in s02 and 'ctx.set_flag("cuda"' in s02
    assert 'flags.get("llama_pair")' in source("installer/stage2/verify_downloads.py")
    assert 'flags.get("llama_pair")' in source("installer/stage2/web/report.py")
    cuda_read = re.compile(r"""(?<!\w)flags?(?:\.get)?\(\s*["']cuda["']|(?<!\w)flags\[\s*["']cuda["']\]""")
    assert [rel for rel, text in py_sources("installer").items() if cuda_read.search(text)] == []
    # No `ns_*` package reads the pair or the CUDA version; only doctor.py (the diagnostics zip) and
    # config_snapshot.py (F-10a: the settings copy for `ns update --rollback`) touch the flags file, and both
    # only copy its bytes.
    ns = py_sources("ns_common", "ns_dashboard", "ns_studio", "ns_supervisor", "ns_tts")
    assert [rel for rel, text in ns.items() if "llama_pair" in text] == []
    assert [rel for rel, text in ns.items() if "wizard_flags" in text] == [
        "ns_supervisor/config_snapshot.py",
        "ns_supervisor/doctor.py",
    ]
    assert '_add_file(z, flags, "setup/wizard_flags.json", skipped)' in ns["ns_supervisor/doctor.py"]
    from ns_common.models import InstallState

    assert "cuda" not in InstallState.model_fields and "llama_pair" not in InstallState.model_fields


def test_dashboard_integration_reason_says_the_calls_were_replaced_not_never_made() -> None:
    status = status_of("CR-C9-5")
    assert "never called these names" not in status
    assert "no longer calls these names" not in status
    assert "no longer calls five of these names" in status and "PR 41 replaced PR 12's lazy calls" in status
    # It still uses three of the names, in the places the Status line gives.
    assert 'lazy("ns_supervisor.client", "SupervisorClient")' in source("ns_dashboard/adapters.py")
    assert 'lazy("ns_supervisor.verify", "run_verify")' in source("ns_dashboard/adapters.py")
    assert "from ns_supervisor.client import SupervisorClient" in source("ns_dashboard/supervisor_link.py")
    assert 'FORMAT_STAGES["clip"]' in source("ns_dashboard/studio_bridge.py")
    for phrase in ("`SupervisorClient`", "`run_verify`", 'FORMAT_STAGES["clip"]', "`clip_stage_names`"):
        assert phrase in status, phrase
    assert "def make_acquirer" in source("ns_dashboard/supervisor_link.py")
    assert "def clip_stage_names" in source("ns_dashboard/studio_bridge.py")
    # The effect: today's adapters do not make the lazy calls PR 12 made, and none of the names is defined.
    adapters = source("ns_dashboard/adapters.py")
    for module, attr in (
        ("ns_studio.orchestrator", "run_job_with_progress"),
        ("ns_studio.clipper", "run_clip_job"),
        ("ns_studio.learn", "import_csv"),
        ("ns_studio.learn", "summary"),
        ("ns_supervisor.doctor", "make_bundle"),
        ("ns_supervisor.scheduler", "set_daily_time"),
    ):
        assert f'lazy("{module}", "{attr}")' not in adapters
    defined = re.compile(
        r"^\s*(?:async )?def (run_job_with_progress|run_clip_job|make_bundle|set_daily_time)\b", re.M
    )
    for rel, text in py_sources("ns_studio", "ns_supervisor", "ns_common").items():
        assert not defined.search(text), f"{rel} defines one of the names CR-C9-5 asked for"
    for rel, text in py_sources("ns_studio/learn").items():
        assert not re.search(r"^def (import_csv|summary)\b", text, re.M), rel
    # What the reason says the dashboard calls instead, and what landed under the requested names.
    bridge = source("ns_dashboard/studio_bridge.py")
    for name in ("run_story_jobs", "run_clip", "run_series_part"):
        assert re.search(rf"^def {name}\(", bridge, re.M)
    assert 'FORMAT_STAGES["clip"] =' in source("ns_studio/clipper/stages_clip.py")
    assert "class SupervisorClient" in source("ns_supervisor/client.py")
    assert re.search(r"^def run_verify\(", source("ns_supervisor/verify.py"), re.M)


def test_dashboard_job_entry_gives_the_real_schedule_path() -> None:
    status = status_of("CR-C9-2")
    assert "reads `params_json.params.schedule_at`" in status
    assert "reads `params_json.schedule_at`" not in status
    assert '["params"]["schedule_at"]' in source("ns_supervisor/scheduled.py")


def test_job_record_note_lists_what_to_dict_returns() -> None:
    note = next(line for line in entry("CR-C9-2").splitlines() if line.startswith("- **Note:**"))
    for phrase in ("`stage`", "`step`", "`steps`", "`public_params`", "`script_words`", "`result`",
                   "`cancel_requested`", "`schedule_at`", "`dismissed_at`"):  # fmt: skip
        assert phrase in note, phrase
    jobs = source("ns_dashboard/jobs.py")
    to_dict = jobs.split("def to_dict(self)")[1].split("\ndef ")[0]
    for key in (
        "stage",
        "step",
        "steps",
        "params",
        "result",
        "cancel_requested",
        "schedule_at",
        "dismissed_at",
    ):
        assert f'"{key}":' in to_dict, key
    assert "public_params(self.params)" in to_dict and '"script_words"' in jobs


def test_first_gpu_dll_request_is_marked_partly_applied_with_what_remains() -> None:
    status = status_of("CR-C6-1", 0)
    assert status.startswith("- **Status:** partly applied")
    assert "`ns_studio/media/transcribe.py`" in status and "`ns_common/gpu_dlls.py`" in status
    assert "`cuda_nvrtc`" in status  # the one folder the common helper does not put on PATH
    # The effects: the common helper exists and is used, the copy is still there and still called.
    gpu = source("ns_common/gpu_dlls.py")
    assert "def ensure_on_path(venv_site_packages" in gpu and "cuda_nvrtc" not in gpu
    assert "gpu_dlls.ensure_on_path()" in source("ns_supervisor/verify.py")
    assert "gpu_dlls.ensure_on_path()" in source("installer/stage2/whisper_probe.py")
    transcribe = source("ns_studio/media/transcribe.py")
    assert "def ensure_gpu_dlls_on_path" in transcribe and '"cuda_nvrtc"' in transcribe
    assert "ensure_gpu_dlls_on_path()" in transcribe


def test_series_routes_are_credited_to_the_library_series_tab() -> None:
    status = status_of("CR-C11-2")
    assert "Library > Stories" not in status
    assert "Library > Series" in status
    library = source("ns_dashboard/ui/src/pages/Library.tsx")
    assert '{ id: "series", label: "Series"' in library
    assert 'api.get("/library/series")' in library and "`/library/series/${encodeURIComponent" in library
    # The Stories tab is another page (every written story with its score) and never calls the series routes.
    assert "/library/series" not in library.split("function Stories()")[1].split("function ")[0]


def test_hygiene_wording_does_not_say_the_test_bans_only_shell_true() -> None:
    hygiene = source("tests/test_hygiene.py")
    assert "httpx" not in hygiene and "TID251" not in hygiene  # no allow-list of either kind
    assert "def test_no_shell_true" in hygiene
    for title in ("CR-C9-4", "CR-C9-8"):
        status = status_of(title)
        assert "bans only `shell=True`" not in status, title
        assert "only `subprocess` rule" in status, title
    # The CSP claim: sent on every response that is not an API response.
    assert "non-API" in status_of("CR-C9-4")
    server = source("ns_dashboard/server.py")
    assert re.search(
        r'if not path\.startswith\("/api/"\):\s+extra\.append\(\(b"content-security-policy"', server
    )


def test_voice_point_three_names_the_routes_it_is_true_for() -> None:
    status = status_of("CR-C9-1", 1)
    assert "`POST /api/voices/design`" in status and "`POST /api/voices/clone`" in status
    assert "`apply_voice`" in status
    assert "synchronous" in source("ns_dashboard/svc_voices.py")
    assert '"apply_voice"' in source("ns_dashboard/jobs.py")
    from ns_common.models import VoiceMakeResponse

    assert list(VoiceMakeResponse.model_fields) == ["voice_id", "preview_wav_path"]


def test_format_option_readers_are_named_as_the_helpers_they_are() -> None:
    status = status_of("CR-C6-6")
    assert "read `channel.captions.mask_profanity` and `channel.formats_allowed` directly" not in status
    assert "`mask_profanity_setting` (`ns_studio/media/caption_plan.py`)" in status
    assert "`formats_allowed` (`ns_studio/media/formats.py`)" in status
    assert "return channel.captions.mask_profanity" in source("ns_studio/media/caption_plan.py")
    assert "return list(channel.formats_allowed) or None" in source("ns_studio/media/formats.py")


def test_ui_prefs_reader_is_the_settings_route() -> None:
    status = status_of("CR-C1-UI-8")
    assert "on the first dashboard start" not in status
    assert "`GET /api/settings`" in status and "`get_settings`" in status
    settings = source("ns_dashboard/svc_settings.py")
    assert "prefs = load_ui_prefs(self.s.paths)" in settings
    assert '"owner_name": prefs["owner_name"]' in settings and '"ui_theme": prefs["theme"]' in settings
    assert '@app.get("/api/settings")' in source("ns_dashboard/server.py")
    # The effect: the page reads the route on start, shows the name, applies the theme if none is saved.
    assert "`ns_dashboard/ui/src/lib/app.tsx`" in status and "`ns-theme`" in status
    app = source("ns_dashboard/ui/src/lib/app.tsx")
    assert 'api.get("/settings")' in app and "setOwnerName(s.owner_name)" in app
    assert 'store.get<Theme | null>("ns-theme", null) === null) setTheme(s.ui_theme)' in app
