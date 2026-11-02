"""C1: the owner-named story model: the HEAD check, the answers, the plan, the download and runtime.json."""

import http.server
import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from installer.stage2 import downloader as dl
from installer.stage2.answers import AnswersError, build_answers
from installer.stage2.runtime_cfg import build_runtime
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s03_download_models import S03DownloadModels
from installer.stage2.sysinfo import required_free_gb as wizard_free_gb
from installer.stage2.sysinfo import sample_preflight
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view, modelcheck, plan
from ns_common.config import load_manifest, load_runtime
from ns_common.models import Answers, LlmCustom

ROOT = Path(__file__).resolve().parents[1]
CUSTOM = {"repo": "bartowski/Qwen2.5-14B-Instruct-GGUF", "file": "Qwen2.5-14B-Instruct-Q4_K_M.gguf",
          "size": 8_988_110_976}


def ctx_with_custom(tmp_path: Path, **over):
    raw = example_answers(llm_custom=dict(CUSTOM), **over)
    return make_test_context(tmp_path, raw_answers=raw, resolve=True)


# ---------------------------------------------------------------------------------------------- contract
def test_llm_custom_is_an_optional_strict_answers_field():
    base = {"channel": {"display_name": "x", "niche": "y"}}
    assert Answers.model_validate(base).llm_custom is None
    ans = Answers.model_validate({**base, "llm_custom": CUSTOM})
    assert isinstance(ans.llm_custom, LlmCustom) and ans.llm_custom.size == CUSTOM["size"]
    for bad in ({**CUSTOM, "size": 0}, {**CUSTOM, "repo": "no-slash"}, {**CUSTOM, "extra": 1},
                {"repo": CUSTOM["repo"], "file": CUSTOM["file"]}):
        with pytest.raises(ValidationError):
            Answers.model_validate({**base, "llm_custom": bad})


def test_the_example_answers_ship_llm_custom_as_null():
    example = json.loads((ROOT / "installer" / "answers.example.json").read_text("utf-8"))
    assert "llm_custom" in example and example["llm_custom"] is None


def test_ns_w113_is_published_with_plain_text():
    codes = json.loads((ROOT / "contracts" / "errors.json").read_text("utf-8"))
    assert "checksum" in codes["NS-W113"]["message"] and codes["NS-W113"]["hint"]
    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    assert copy["NS-W113"]["title"] and copy["NS-W113"]["whatToDo"]
    assert "NS-W113" in (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text("utf-8")


# ---------------------------------------------------------------------------------------------- the check
def head_of(status: int, size: int | None):
    seen: list[str] = []

    def head(url: str) -> tuple[int, int | None]:
        seen.append(url)
        return status, size

    head.seen = seen  # type: ignore[attr-defined]
    return head


def test_a_good_file_resolves_its_size_from_the_hugging_face_url():
    head = head_of(200, 9_000_000_000)
    out = modelcheck.check_model("  Qwen/Qwen3-14B-GGUF ", "Qwen3-14B-Q4_K_M.gguf", head)
    assert out == {"ok": True, "size": 9_000_000_000, "message": None, "code": None}
    assert head.seen == ["https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf"]


def test_a_file_over_15_5_gb_is_refused_with_the_reason_and_its_size():
    out = modelcheck.check_model("a/b", "model-Q8_0.gguf", head_of(200, 17_400_000_000))
    assert out["ok"] is False and out["size"] == 17_400_000_000
    assert "17.4 GB" in out["message"] and "15.5 GB" in out["message"] and "smaller" in out["message"]
    edge = modelcheck.check_model("a/b", "model-Q8_0.gguf", head_of(200, modelcheck.MAX_BYTES))
    assert edge["ok"] is True                                       # exactly 15.5 GB still fits
    assert modelcheck.check_model("a/b", "m.gguf", head_of(200, modelcheck.MAX_BYTES + 1))["ok"] is False


@pytest.mark.parametrize(
    ("status", "size", "words"),
    [(404, None, "not found"), (401, None, "login"), (403, None, "login"), (500, None, "could not reach"),
     (200, None, "how big")],
)
def test_every_refusal_says_what_to_do(status, size, words):
    out = modelcheck.check_model("a/b", "m.gguf", head_of(status, size))
    assert out["ok"] is False and words in out["message"].lower()


def test_an_unreachable_site_is_the_network_code():
    def boom(url: str):
        raise OSError("no route")

    out = modelcheck.check_model("a/b", "m.gguf", boom)
    assert out["ok"] is False and out["code"] == "NS-E105"


@pytest.mark.parametrize(
    ("repo", "file", "words"),
    [("justone", "m.gguf", "owner/name"), ("a/b", "model.bin", "GGUF file name"),
     ("a/b", "", "GGUF file name"),
     ("a/b", "../x.gguf", "GGUF file name"), ("a b/c", "m.gguf", "owner/name"),
     ("a/b", "m-00001-of-00003.gguf", "Split files")],
)
def test_a_name_that_cannot_be_right_is_refused_before_any_request(repo, file, words):
    head = head_of(200, 1)
    out = modelcheck.check_model(repo, file, head)
    assert out["ok"] is False and words in out["message"] and head.seen == []


def test_a_file_in_a_subfolder_keeps_its_path_in_the_url():
    head = head_of(200, 5_000_000_000)
    assert modelcheck.check_model("a/b", "sub/dir/m.Q4.gguf", head)["ok"] is True
    assert head.seen == ["https://huggingface.co/a/b/resolve/main/sub/dir/m.Q4.gguf"]


def test_the_simulate_head_is_deterministic_and_covers_the_refusals():
    assert modelcheck.fake_head("https://x/y/resolve/main/m-Q4_K_M.gguf") == (200, 9_000_000_000)
    assert modelcheck.fake_head("https://x/y/resolve/main/m-q8_0.gguf") == (200, 14_600_000_000)
    assert modelcheck.fake_head("https://x/y/resolve/main/m-70B-Q4.gguf")[1] > modelcheck.MAX_BYTES
    assert modelcheck.fake_head("https://x/y/resolve/main/missing.gguf")[0] == 404
    assert modelcheck.fake_head("https://x/y/resolve/main/private.gguf")[0] == 401


def serve_heads():
    """A loopback server that answers HEAD like Hugging Face: a redirect with x-linked-size, a 200, a 404."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return None

        def do_HEAD(self) -> None:  # noqa: N802 - http.server API
            seen.append(self.path)
            if self.path == "/linked":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/never-followed")
                self.send_header("X-Linked-Size", "4242")
            elif self.path == "/plain":
                self.send_response(200)
                self.send_header("Content-Length", "777")
            else:
                self.send_response(404)
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_the_stdlib_head_reads_x_linked_size_without_following_the_redirect():
    server, seen = serve_heads()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        assert modelcheck.urllib_head(base + "/linked") == (200, 4242)
        assert modelcheck.urllib_head(base + "/plain") == (200, 777)
        assert modelcheck.urllib_head(base + "/gone") == (404, None)
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["/linked", "/plain", "/gone"]                  # the redirect target was never asked


# ---------------------------------------------------------------------------------------------- the form
def good_draft(**over):
    data = {
        "ownerName": "Abdo", "channel": {"displayName": "After Dark Tales", "niche": "horror fiction",
                                         "language": "en"},
        "homePath": "D:\\Nightshift", "llmTier": "small", "llmCustom": dict(CUSTOM),
        "voice": {"speaker": "Ryan"}, "modelsSource": {"kind": "internet", "path": None}, "runTime": "03:00",
        "mastersPerWeek": 1, "installCode": True, "theme": "dark", "pexelsKey": "pk-test",
        "pixabayKey": "xk-test",
    }
    data.update(over)
    return data


def make_env(**over):
    ram = over.pop("ram_gb", 32.0)
    tiers = plan.tier_infos(plan.load_manifest(ROOT), "gpu", ram_gb=ram, vram_mb=16311)
    args = {"tiers": tiers, "drive_letters": {"D"}, "free_gb": lambda _p: 500.0, "keys_stored": {},
            "ram_gb": ram}
    args.update(over)
    return answers_view.Env(**args)


def test_a_resolved_custom_model_passes_and_is_written_as_llm_custom_with_the_small_tier():
    assert answers_view.validate(good_draft(), make_env()) == []
    raw = answers_view.draft_to_answers(good_draft(llmTier="best"))
    assert raw["llm_custom"] == CUSTOM and raw["llm_tier"] == "small"       # a custom model replaces the tier
    ans = build_answers(dict(raw))
    assert ans.llm_custom is not None and ans.llm_custom.file == CUSTOM["file"]
    assert answers_view.draft_to_answers(good_draft(llmCustom=None))["llm_custom"] is None
    view = answers_view.answers_to_view(raw)
    assert view["llmCustom"] == CUSTOM
    assert answers_view.answers_to_view({"llm_tier": "best"})["llmCustom"] is None


def test_a_custom_model_has_no_ram_rule_but_does_not_hide_a_bad_entry():
    low_ram = make_env(ram_gb=16.0)
    # the tier is replaced by the custom file, so its RAM rule does not apply
    assert answers_view.validate(good_draft(llmTier="balanced"), low_ram) == []
    errs = {e["field"]: e["message"] for e in answers_view.validate(
        good_draft(llmCustom={**CUSTOM, "size": 16_000_000_000}), make_env())}
    assert "16.0 GB" in errs["llmCustom"] and "15.5 GB" in errs["llmCustom"]
    bad_entries = ({"repo": "x", "file": CUSTOM["file"], "size": 5}, {**CUSTOM, "size": 0}, "a/b",
                   {**CUSTOM, "size": True})
    for bad in bad_entries:
        got = {e["field"] for e in answers_view.validate(good_draft(llmCustom=bad), make_env())}
        assert "llmCustom" in got, bad


def test_the_free_space_rule_follows_the_custom_size():
    assert plan.required_free_gb("small", None) == 60 == wizard_free_gb("small", None)
    for size in (5_000_000_000, 9_000_000_000, 12_100_000_000, 15_500_000_000):
        assert plan.required_free_gb("small", size) == wizard_free_gb("small", size)
    assert plan.required_free_gb("small", 15_500_000_000) == 67          # 60 + (16 - 9)
    assert plan.required_free_gb("small", 5_000_000_000) == 60           # never below the base
    env = make_env(free_gb=lambda _p: 62.0)
    draft = good_draft(llmCustom={**CUSTOM, "size": 15_000_000_000})
    errs = {e["field"] for e in answers_view.validate(draft, env)}
    assert "homePath" in errs                                              # 62 GB free, 66 GB needed


def test_build_answers_enforces_the_limit_and_the_name_for_unattended_files():
    base = {"channel": {"display_name": "x", "niche": "y"}}
    assert build_answers({**base, "llm_custom": CUSTOM}).llm_custom is not None
    with pytest.raises(AnswersError, match="15.5 GB"):
        build_answers({**base, "llm_custom": {**CUSTOM, "size": 20_000_000_000}})
    with pytest.raises(AnswersError, match="GGUF file name"):
        build_answers({**base, "llm_custom": {**CUSTOM, "file": "weights.safetensors"}})


# ---------------------------------------------------------------------------------------------- the plan
def test_the_plan_row_and_totals_use_the_custom_file():
    raw = plan.load_manifest(ROOT)
    p = plan.build_plan(raw, profile="gpu", tier="small", include_code=True, cuda="13.0", ram_gb=32.0,
                        vram_mb=16311, custom=CUSTOM)
    models = next(g for g in p["groups"] if g["id"] == "models")
    first = models["items"][0]
    assert first["file"] == CUSTOM["file"] and first["bytes"] == CUSTOM["size"]
    assert first["label"] == "Story model (Custom)"
    small = plan.build_plan(raw, profile="gpu", tier="small", include_code=True, cuda="13.0", ram_gb=32.0,
                            vram_mb=16311)
    assert p["totalBytes"] == small["totalBytes"] - 9_001_752_960 + CUSTOM["size"]
    assert p["needFreeGb"] == 60 and small["needFreeGb"] == 60


def test_the_download_plan_takes_the_custom_file_without_a_hash(tmp_path: Path):
    manifest = load_manifest(ROOT / "manifest.json")
    custom = LlmCustom.model_validate(CUSTOM)
    items = dl.plan_models(manifest, "gpu", "small", tmp_path, custom=custom)
    first = items[0]
    assert first.name == CUSTOM["file"] and first.sha256 is None and first.size == CUSTOM["size"]
    assert first.url == f"https://huggingface.co/{CUSTOM['repo']}/resolve/main/{CUSTOM['file']}"
    assert first.dest == tmp_path / "models" / "llm" / CUSTOM["file"]
    assert items[1].name == "nomic-embed-text-v1.5.Q8_0.gguf" and items[1].sha256   # the rest stays pinned
    plain = dl.plan_models(manifest, "gpu", "small", tmp_path)
    assert plain[0].sha256 and [i.name for i in items[1:]] == [i.name for i in plain[1:]]
    both = dl.plan_from_manifest(manifest, "gpu", "small", tmp_path, custom=custom)
    assert CUSTOM["file"] in {i.name for i in both}


# ---------------------------------------------------------------------------------------------- wizard steps
def test_s01_keeps_the_custom_model_and_ignores_the_tier_ram_rule(tmp_path: Path):
    pf = sample_preflight(tmp_path / "home", ROOT)
    pf["ram_gb"] = 16
    raw = example_answers(home_path=str(tmp_path / "home"), llm_tier="balanced", llm_custom=dict(CUSTOM))
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False), preflight=pf)
    S01Questions().run(ctx)
    assert ctx.answers is not None and ctx.answers.llm_tier == "small"
    assert ctx.answers.llm_custom is not None and ctx.answers.llm_custom.size == CUSTOM["size"]
    saved = json.loads((ctx.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["llm_custom"] == CUSTOM
    assert "custom: " + CUSTOM["file"] in ctx.ui.text or any(CUSTOM["file"] in str(t) for t in ctx.ui.tables)


def test_s01_refuses_a_custom_model_that_is_too_big(tmp_path: Path):
    raw = example_answers(home_path=str(tmp_path / "home"), llm_custom={**CUSTOM, "size": 30_000_000_000})
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    with pytest.raises(AnswersError, match="15.5 GB"):
        S01Questions().run(ctx)


def test_s03_downloads_the_custom_file_with_a_warning_and_points_runtime_json_at_it(tmp_path: Path):
    ctx = ctx_with_custom(tmp_path)
    step = S03DownloadModels()
    assert step.check(ctx) is False
    step.run(ctx)
    step.verify(ctx)
    target = ctx.paths.models_llm / CUSTOM["file"]
    assert target.is_file() and not (ctx.paths.models_llm / "Qwen3-14B-Q4_K_M.gguf").exists()
    assert [c for c, _ in ctx.warnings] == ["NS-W113"]
    assert CUSTOM["repo"] in ctx.warnings[0][1]
    assert step.check(ctx) is True


def test_s03_repoints_an_existing_runtime_json(tmp_path: Path):
    ctx = ctx_with_custom(tmp_path)
    from ns_common.config import save_runtime
    from ns_common.models import Runtime

    rt = Runtime.default("gpu")
    rt.llama.model_path = str(ctx.paths.models_llm / "Qwen3-14B-Q4_K_M.gguf")
    save_runtime(ctx.paths, rt)
    S03DownloadModels().run(ctx)
    assert load_runtime(ctx.paths).llama.model_path == str(ctx.paths.models_llm / CUSTOM["file"])


def test_s03_without_a_custom_model_raises_no_warning(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    S03DownloadModels().run(ctx)
    assert ctx.warnings == []


def test_the_baseline_runtime_points_at_the_custom_file_with_plain_flags(tmp_path: Path):
    ctx = ctx_with_custom(tmp_path, llm_tier="best")                  # whatever tier was left in the file
    rt = build_runtime(ctx)
    assert rt.llama.model_path == str(ctx.paths.models_llm / CUSTOM["file"])
    assert rt.llama.flags.spec_type is None and rt.llama.flags.n_cpu_moe in (None, 0)
    assert rt.llama.flags.kv_type == "f16" and rt.llama.flags.ctx == 16384
