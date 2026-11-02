"""C1: the Choices form: validation rules, camelCase <-> snake_case, keys never written to disk."""

import json
from pathlib import Path

import pytest

from installer.stage2.answers import InstallerAnswers, build_answers
from installer.stage2.web import answers_view, plan
from ns_common.models import Answers

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))


def good_draft(**over) -> dict:
    draft = {
        "ownerName": "Abdo",
        "channel": {"displayName": "After Dark Tales", "niche": "horror fiction", "language": "en"},
        "homePath": "D:\\Nightshift", "llmTier": "small", "voice": {"speaker": "Ryan"},
        "modelsSource": {"kind": "internet", "path": None}, "runTime": "03:00", "mastersPerWeek": 1,
        "installCode": True, "theme": "dark", "pexelsKey": "pk-test", "pixabayKey": "xk-test",
    }
    draft.update(over)
    return draft


def make_env(ram_gb: float = 32.0, free: float | None = 412.0, **kw) -> answers_view.Env:
    manifest = plan.load_manifest(ROOT)
    tiers = plan.tier_infos(manifest, "gpu", ram_gb=ram_gb, vram_mb=16311)
    args = {"ram_gb": ram_gb, "tiers": tiers, "drive_letters": {"C", "D"}, "free_gb": lambda _p: free,
            "isdir": lambda _p: True, "keys_stored": {}}
    args.update(kw)
    return answers_view.Env(**args)


def fields(errors: list[dict]) -> dict[str, str]:
    return {e["field"]: e["message"] for e in errors}


def test_a_good_draft_has_no_errors():
    assert answers_view.validate(good_draft(), make_env()) == []


@pytest.mark.parametrize(
    ("over", "field", "message"),
    [
        ({"channel": {"displayName": "  ", "niche": "horror fiction"}}, "channel.displayName",
         "Give the channel a name"),
        ({"channel": {"displayName": "x" * 61, "niche": "horror fiction"}}, "channel.displayName",
         "Give the channel a name"),
        ({"channel": {"displayName": "After Dark", "niche": "x"}}, "channel.niche",
         "Say in a few words what the channel is about"),
        ({"ownerName": "y" * 41}, "ownerName", "Use 40 characters or fewer"),
        ({"runTime": "25:00"}, "runTime", "Use the 24 hour clock, for example 03:00"),
        ({"runTime": "3:00"}, "runTime", "Use the 24 hour clock, for example 03:00"),
        ({"pexelsKey": ""}, "pexelsKey", "Paste the Pexels key. Setup needs it to find stock footage"),
        ({"pixabayKey": None}, "pixabayKey", "Paste the Pixabay key. Setup needs it to find stock footage"),
        ({"voice": {"speaker": "Nobody"}}, "voice.speaker", "Pick one of the four narrators"),
        ({"mastersPerWeek": 4}, "mastersPerWeek", "Pick 1, 2 or 3 long videos per week"),
    ],
)
def test_field_rules_and_their_plain_messages(over, field, message):
    assert fields(answers_view.validate(good_draft(**over), make_env()))[field] == message


def test_a_stored_key_may_stay_empty_on_resume():
    env = make_env(keys_stored={"pexelsKey": True, "pixabayKey": True})
    assert answers_view.validate(good_draft(pexelsKey=None, pixabayKey=None), env) == []


def test_home_path_rules():
    env = make_env(free=31.0)
    err = fields(answers_view.validate(good_draft(), env))
    assert err["homePath"] == "Pick a folder on a drive with 60 GB free. This one has 31 GB"
    for bad in ("relative\\path", "D:Nightshift", "%TEMP%\\x", "\\\\server\\share", ""):
        assert "homePath" in fields(answers_view.validate(good_draft(homePath=bad), make_env())), bad
    assert "homePath" in fields(answers_view.validate(good_draft(homePath="D:\\" + "a" * 90), make_env()))
    assert "homePath" in fields(answers_view.validate(good_draft(homePath="Z:\\Nightshift"), make_env()))
    # the balanced tier needs 74 GB free
    env = make_env(free=70.0)
    assert "homePath" in fields(answers_view.validate(good_draft(llmTier="balanced"), env))
    assert "homePath" not in fields(answers_view.validate(good_draft(), env))


def test_tier_must_be_allowed_on_this_pc():
    err = fields(answers_view.validate(good_draft(llmTier="balanced"), make_env(ram_gb=16.0)))
    assert err["llmTier"] == "Balanced needs 32 GB of memory. This PC has 16 GB"
    assert "llmTier" not in fields(answers_view.validate(good_draft(llmTier="best"), make_env(ram_gb=32.0)))
    assert "llmTier" in fields(answers_view.validate(good_draft(llmTier="huge"), make_env()))


def test_models_folder_must_exist_unless_the_source_is_the_internet():
    folder = {"kind": "folder", "path": "E:\\models"}
    missing = answers_view.validate(good_draft(modelsSource=folder), make_env(isdir=lambda _p: False))
    assert fields(missing)["modelsSource.path"] == "That folder was not found"
    assert answers_view.validate(good_draft(modelsSource=folder), make_env()) == []
    zips = {"kind": "drive-zip", "path": None}
    assert "modelsSource.path" in fields(answers_view.validate(good_draft(modelsSource=zips), make_env()))
    internet = {"kind": "internet", "path": "ignored"}
    assert answers_view.validate(good_draft(modelsSource=internet), make_env()) == []


def test_draft_to_answers_is_snake_case_and_never_holds_a_key():
    raw = answers_view.draft_to_answers(good_draft(modelsSource={"kind": "drive-zip", "path": "E:\\zips"}))
    assert set(raw) == set(EXAMPLE)                      # the same keys as installer/answers.example.json
    assert raw["models_source_kind"] == "drive_zip" and raw["models_source"] == "E:\\zips"
    assert raw["pexels_api_key"] == "" and raw["pixabay_api_key"] == ""
    assert raw["owner_name"] == "Abdo" and raw["ui_theme"] == "dark" and raw["llm_tier"] == "small"
    assert raw["channel"]["display_name"] == "After Dark Tales" and raw["voice"]["speaker"] == "Ryan"
    assert "pk-test" not in json.dumps(raw) and "xk-test" not in json.dumps(raw)
    internet = answers_view.draft_to_answers(good_draft(modelsSource={"kind": "internet", "path": "E:\\x"}))
    assert internet["models_source"] is None and internet["models_source_kind"] == "internet"


def test_what_the_server_writes_is_accepted_by_the_wizard_and_the_contract():
    raw = answers_view.draft_to_answers(good_draft(theme="light", ownerName="  "))
    assert raw["owner_name"] is None
    ans = build_answers(dict(raw))
    assert isinstance(ans, InstallerAnswers) and ans.ui_theme == "light" and ans.llm_tier == "small"
    Answers.model_validate({k: v for k, v in raw.items() if k in Answers.model_fields})
    zip_raw = answers_view.draft_to_answers(good_draft(modelsSource={"kind": "drive-zip", "path": "E:\\z"}))
    assert build_answers(dict(zip_raw)).models_source_kind == "drive_zip"


def test_round_trip_with_the_example_answers():
    view = answers_view.answers_to_view(EXAMPLE)
    assert view["channel"]["displayName"] == "After Dark Tales" and view["voice"]["speaker"] == "Ryan"
    assert view["modelsSource"] == {"kind": "internet", "path": None}
    assert view["pexelsKeySet"] is False and view["homePath"] == EXAMPLE["home_path"]
    draft = {k: v for k, v in view.items() if k not in ("pexelsKeySet", "pixabayKeySet")}
    draft["pexelsKey"], draft["pixabayKey"] = "a", "b"
    back = answers_view.draft_to_answers(draft)
    for key in ("home_path", "llm_tier", "run_time", "masters_per_week", "install_code", "owner_name"):
        assert back[key] == EXAMPLE[key], key
    assert back["channel"]["display_name"] == EXAMPLE["channel"]["display_name"]
    assert answers_view.kind_to_file("drive-zip") == "drive_zip"
    assert answers_view.kind_from_file("drive_zip") == "drive-zip"


def test_write_answers_blanks_the_keys_and_reads_back(tmp_path: Path):
    secret = dict(EXAMPLE, pexels_api_key="secret-pexels", pixabay_api_key="secret-pixabay")
    target = answers_view.write_answers(tmp_path / "config" / "answers.json", secret)
    text = target.read_text(encoding="utf-8")
    assert "secret-pexels" not in text and "secret-pixabay" not in text
    assert answers_view.read_answers(target)["pexels_api_key"] == ""
    found = answers_view.keys_in(secret)
    assert found == {"pexels_api_key": "secret-pexels", "pixabay_api_key": "secret-pixabay"}
    assert answers_view.keys_in(EXAMPLE) == {"pexels_api_key": None, "pixabay_api_key": None}


def test_read_answers_accepts_a_bom_and_rejects_a_list(tmp_path: Path):
    f = tmp_path / "a.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"home_path": "D:\\x"}).encode("utf-8"))
    assert answers_view.read_answers(f)["home_path"] == "D:\\x"
    f.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError):
        answers_view.read_answers(f)


def test_include_voicedesign_checkbox_reaches_answers_json_and_back():
    on = answers_view.draft_to_answers(good_draft(includeVoicedesign=True))
    assert on["include_voicedesign"] is True and build_answers(dict(on)).include_voicedesign is True
    assert answers_view.answers_to_view(on)["includeVoicedesign"] is True
    off = answers_view.draft_to_answers(good_draft())                       # an old draft without the field
    assert off["include_voicedesign"] is False
    assert answers_view.answers_to_view(off)["includeVoicedesign"] is False


def test_the_committed_installer_build_has_the_voicedesign_checkbox():
    built = (ROOT / "installer" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "includeVoicedesign" in built and "voice design model" in built
