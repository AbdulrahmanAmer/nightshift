# ruff: noqa: E501
"""C1: the setup server in dev mode (docs/DEV_MODE.md): information rows instead of blockers, a 10 GB disk rule,
a plan that lists stubs, the banner in the state, `--dev` on the wizard's command line."""

import json
import time
from pathlib import Path

import pytest

from installer.stage2 import devmode
from installer.stage2.web import answers_view, controller, plan, preflight
from installer.stage2.web.__main__ import parse_args
from installer.stage2.web.controller import Config, SetupController, read_install_state
from installer.stage2.web.engine import wizard_argv
from installer.stage2.web.fakes import ScriptedEngine, fake_effects, sample_handover
from tests.test_installer_web_controller import draft, finish_script

ROOT = Path(__file__).resolve().parents[1]


def laptop(home: str = r"C:\Nightshift", **over) -> dict:
    """The hand-over of an 8 GB laptop with no NVIDIA card and 30 GB free (what stage 1 writes without -Dev)."""
    pf = sample_handover(ROOT, home=home)
    pf.update(profile="cpu-dev", llama_pair="", cuda="", ram_gb=8.0, gpu={"name": None, "vram_mb": 0, "driver": None},
              drives=[{"letter": home[0].upper(), "free_gb": 30.0, "type": "SSD"}])
    for c in pf["checks"]:
        if c["id"] == "gpu":
            c.update(status="fail", code="NS-E102", value="nvidia-smi found no GPU")
        elif c["id"] == "driver":
            c.update(status="warn", value="not checked")
        elif c["id"] == "ram":
            c.update(status="warn", value="8 GB is below the 16 GB minimum")
        elif c["id"] == "disk":
            c.update(status="fail", code="NS-E104", value="30 GB free on C:")
    pf.update(over)
    return pf


def row(rows: list, row_id: str) -> dict:
    return next(r for r in rows if r["id"] == row_id)


# ---------------------------------------------------------------------------------------------- preflight
def test_apply_dev_turns_the_gpu_driver_and_memory_rows_into_information_rows():
    out = preflight.apply_dev(laptop())
    rows = preflight.check_rows(out)
    assert [r["status"] for r in rows if r["id"] in ("gpu", "driver", "ram")] == ["ok", "ok", "ok"]
    assert "no NVIDIA graphics card" in row(rows, "gpu")["value"] and "dev mode" in row(rows, "driver")["value"]
    assert out["dev_mode"] is True and out["profile"] == "cpu-dev" and out["llama_pair"] == ""
    assert not any(r["blocking"] and r["id"] in ("gpu", "driver") for r in rows)
    assert preflight.apply_dev(out) == out                                    # idempotent


def test_apply_dev_names_a_card_that_is_there_and_warns_under_4_gb():
    out = preflight.apply_dev(sample_handover(ROOT))
    assert "NVIDIA GeForce RTX 5060 Ti" in row(out["checks"], "gpu")["value"] and out["llama_pair"] == ""
    low = preflight.apply_dev(laptop(ram_gb=3.0))
    assert row(low["checks"], "ram")["status"] == "warn" and "4 GB" in row(low["checks"], "ram")["value"]


def test_the_disk_rule_of_a_dev_install_is_10_gb_and_a_real_install_keeps_60():
    dev = preflight.apply_dev(laptop())
    assert row(preflight.raw_disk_rows(dev, "C:\\Nightshift"), "disk")["status"] == "ok"
    dev["drives"][0]["free_gb"] = 9.0
    bad = row(preflight.raw_disk_rows(dev, "C:\\Nightshift"), "disk")
    assert bad["status"] == "fail" and bad["code"] == "NS-E104" and "10 GB" in bad["value"]
    assert "Free 10 GB" in row(preflight.check_rows(dev, "C:\\Nightshift"), "disk")["whatToDo"]
    real = laptop()
    assert row(preflight.raw_disk_rows(real, "C:\\Nightshift"), "disk")["status"] == "fail"       # 30 GB
    real["drives"][0]["free_gb"] = 50.0
    assert row(preflight.raw_disk_rows(real, "C:\\Nightshift"), "disk")["status"] == "warn"
    assert "Free 60 GB" in row(preflight.check_rows(laptop(), "C:\\Nightshift"), "disk")["whatToDo"]


def test_the_meters_ask_for_4_gb_of_memory_and_10_of_disk_in_dev_mode():
    dev = preflight.machine_summary(preflight.apply_dev(laptop()), "C:\\Nightshift", "small")
    assert dev["meters"]["ram"] == {"haveGb": 8.0, "needGb": 4, "hardMinGb": 4}
    assert (dev["meters"]["disk"]["needGb"], dev["meters"]["disk"]["hardMinGb"]) == (10, 10)
    real = preflight.machine_summary(laptop(), "C:\\Nightshift", "small")
    assert (real["meters"]["disk"]["needGb"], real["meters"]["ram"]["needGb"]) == (60, 16)


def test_disk_check_and_the_home_validation_use_the_dev_threshold():
    drives = [{"letter": "C", "freeGb": 12.0, "type": "SSD"}]
    assert preflight.disk_check("C:\\Nightshift", "small", drives, lambda p: 12.0, dev=True)["ok"] is True
    assert preflight.disk_check("C:\\Nightshift", "small", drives, lambda p: 12.0)["ok"] is False
    env_dev = answers_view.Env(ram_gb=8, tiers={}, free_gb=lambda p: 12.0, dev=True)
    env_real = answers_view.Env(ram_gb=8, tiers={}, free_gb=lambda p: 12.0)
    assert answers_view._home_errors("C:\\Nightshift", "small", env_dev) == []
    assert answers_view._home_errors("C:\\Nightshift", "small", env_real)[0]["code"] == "NS-E104"


# ---------------------------------------------------------------------------------------------- plan
def test_the_dev_plan_lists_the_cpu_build_and_stubs(tmp_path: Path):
    manifest = plan.load_manifest(ROOT)
    p = plan.build_plan(manifest, profile="gpu", tier="best", include_code=True, cuda="13.0", ram_gb=8.0,
                        vram_mb=None, dev=True)
    items = {i["key"]: i for g in p["groups"] for i in g["items"]}
    assert p["profile"] == "cpu-dev" and p["tier"] == "small" and p["needFreeGb"] == 10
    assert {"llama_cpu", "yt_dlp", "webview2"} <= set(items)
    assert not {"opencode", "cudart", "cudart13", "llama_cuda", "llama_cuda13"} & set(items)
    models = next(g for g in p["groups"] if g["id"] == "models")["items"]
    stubs = [i for i in models if i["key"] != "yunet"]
    assert stubs and all(i["label"].endswith("(stub, dev)") and i["bytes"] == devmode.STUB_BYTES for i in stubs)
    assert items["yunet"]["label"] == "Face finder for Clipper" and p["totalBytes"] < 400e6
    assert p["recommendedTier"] == "small" and p["tiers"]["small"]["allowed"] is True
    assert p["tiers"]["balanced"]["allowed"] is False and p["tiers"]["best"]["reason"] == "Not available in dev mode"
    assert p["tiers"]["small"]["needFreeGb"] == 10 and p["tiers"]["small"]["llmBytes"] == devmode.STUB_BYTES


def test_the_real_cpu_plan_is_not_changed_by_the_dev_code(tmp_path: Path):
    manifest = plan.load_manifest(ROOT)
    p = plan.build_plan(manifest, profile="cpu-dev", tier="small", include_code=True, cuda=None, ram_gb=16.0, vram_mb=None)
    labels = [i["label"] for g in p["groups"] for i in g["items"]]
    assert not any("stub" in x for x in labels) and p["needFreeGb"] == 60 and "Nightshift Code" in labels


# ---------------------------------------------------------------------------------------------- the controller
class DevSetup:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, dev: bool = True, script=None,
                 pf: bool | dict | None = None) -> None:
        monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")
        monkeypatch.delenv("NIGHTSHIFT_HOME")
        monkeypatch.delenv("NIGHTSHIFT_DEV_MODE", raising=False)
        monkeypatch.setattr(answers_view, "MAX_HOME_LEN", 400)
        monkeypatch.setattr(controller, "desktop_dir", lambda: tmp_path / "desktop")
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.home = tmp_path / "home"
        self.fx, self.cmd = fake_effects(tmp_path / "fx")
        self.engine = ScriptedEngine(script) if script else None
        if self.engine:
            self.fx.engine_factory = self.engine.factory()
        self.cfg = Config(app_path=ROOT, data_dir=tmp_path / "data", simulate=True, home=self.home, dev_mode=dev)
        if pf:      # True: a laptop with no card; a dict: the same with these keys changed
            pf_file = tmp_path / "pf.json"
            pf_file.write_text(json.dumps(laptop(str(self.home), **(pf if isinstance(pf, dict) else {}))), encoding="utf-8")
            self.cfg.preflight = pf_file
        self.ctl = SetupController(self.cfg, self.fx)

    def snap(self) -> dict:
        return self.ctl.snapshot()

    def until(self, pred, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred(self.snap()):
                return self.snap()
            time.sleep(0.02)
        raise AssertionError(f"timed out; run={self.snap()['run']}")


def test_the_first_state_of_a_dev_install_on_a_laptop_does_not_block(tmp_path, monkeypatch):
    s = DevSetup(tmp_path, monkeypatch, pf=True)
    state = s.snap()
    assert state["devMode"] is True and state["blocking"] is False
    assert state["machine"]["profile"] == "cpu-dev" and state["plan"]["profile"] == "cpu-dev"
    assert {r["id"]: r["status"] for r in state["checks"]}["gpu"] == "ok"
    keys = {r["key"] for r in state["downloads"]["items"]}
    assert "llama_cpu" in keys and "llama_cuda13" not in keys and "opencode" not in keys
    assert state["plan"]["tier"] == "small" and state["plan"]["needFreeGb"] == 10
    assert state["machine"]["meters"]["disk"]["hardMinGb"] == 10


def test_the_same_laptop_blocks_without_dev(tmp_path, monkeypatch):
    s = DevSetup(tmp_path, monkeypatch, dev=False, pf=True)
    assert s.snap()["devMode"] is False and s.snap()["blocking"] is True


def test_the_environment_variable_and_the_hand_over_flag_each_turn_dev_mode_on(tmp_path, monkeypatch):
    s = DevSetup(tmp_path / "a", monkeypatch, dev=False)
    assert s.snap()["devMode"] is False
    monkeypatch.setenv("NIGHTSHIFT_DEV_MODE", "1")
    t = SetupController(Config(app_path=ROOT, data_dir=tmp_path / "b", simulate=True, home=tmp_path / "hb"), s.fx)
    assert t.snapshot()["devMode"] is True
    monkeypatch.delenv("NIGHTSHIFT_DEV_MODE")
    u = DevSetup(tmp_path / "c", monkeypatch, dev=False, pf={"dev_mode": True})
    assert u.snap()["devMode"] is True


def test_a_whole_dev_run_passes_dev_to_the_wizard_and_reports_stubs(tmp_path, monkeypatch):
    holder: dict = {}

    def extra(e: ScriptedEngine) -> None:
        flags = holder["s"].home / "config"
        flags.mkdir(parents=True, exist_ok=True)
        (flags / "wizard_flags.json").write_text(json.dumps({"schedule": "installed", "schedule_mode": "disabled"}), encoding="utf-8")

    s = DevSetup(tmp_path, monkeypatch, script=lambda e: finish_script(e, extra))
    holder["s"] = s
    result = s.ctl.submit_answers(draft(s.home, installCode=True))
    assert result["ok"], result
    assert s.ctl.do_action("start")[0] == 200
    state = s.until(lambda st: st["run"] == "finished")
    assert "--dev" in s.engine.argv
    res = state["result"]
    assert res["devMode"] is True and res["scheduled"] is False
    details = {i["label"]: i["detail"] for i in res["installed"]}
    assert details["Engines"] == "fake (dev mode)" and "stubs" in details["Story, voice and caption models"]
    assert details["Nightshift Code"].startswith("not installed") and details["Night run"].startswith("off")


def test_the_wizard_command_line_carries_dev_only_in_dev_mode(tmp_path: Path):
    common = dict(app_path=tmp_path, home=tmp_path, answers=tmp_path / "a.json", preflight=tmp_path / "p.json", bridge=True)
    assert wizard_argv("py", **common, dev=True)[-1] == "--dev"
    assert "--dev" not in wizard_argv("py", **common)
    assert parse_args(["--dev"]).dev is True and parse_args([]).dev is False


def test_starting_in_the_other_mode_resets_the_later_steps(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    steps = {sid: {"status": "done", "error": None, "finished_at": "x"} for sid in ("b01_tools", "s01_questions", "s02_download_bins")}
    (home / "config" / "install_state.json").write_text(json.dumps({"steps": steps, "dev_mode": False}), encoding="utf-8")
    DevSetup(tmp_path, monkeypatch, dev=True)
    state = read_install_state(home)
    assert state["dev_mode"] is True and sorted(state["steps"]) == ["b01_tools", "s01_questions"]
