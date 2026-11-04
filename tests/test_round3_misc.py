"""Studio round 3, the smaller items: reject moves files, updater wording and file install, GitHub errors,
the whisper code, the embed server's view of the card, one slot."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

from ns_common.errors import NsError, describe
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_dashboard.updater import classify_update, error_line, parse_update_output
from ns_supervisor import releases

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------- Reject moves the files


def make(tmp_home: Path) -> Dashboard:
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    seed_channel(state.db, state.paths, "after-dark")
    return Dashboard(state)


def test_reject_moves_the_video_and_its_sheet_out_of_the_outbox(tmp_home: Path):
    dash = make(tmp_home)
    paths = dash.state.paths
    oid = seed_output(dash.state.db, paths, title="The Doorbell Camera")
    before = Path(dash.outputs.detail(oid)["path"])
    assert before.exists() and paths.outbox in before.parents
    sheets = [p for p in before.parent.glob(f"{before.stem}.*") if p.is_file()]
    assert len(sheets) >= 2  # the video and at least one sheet

    rej = dash.outputs.reject(oid, "Hook is boring")
    assert rej["status"] == "rejected" and rej["review_note"] == "Hook is boring"
    after = Path(rej["path"])
    assert after.exists() and paths.rejected in after.parents and paths.outbox not in after.parents
    assert not before.exists()  # the video is no longer among the ones waiting
    assert all(not p.exists() for p in sheets)
    assert {p.name for p in after.parent.glob(f"{after.stem}.*")} == {p.name for p in sheets}


def test_a_second_rejection_only_changes_the_note(tmp_home: Path):
    dash = make(tmp_home)
    oid = seed_output(dash.state.db, dash.state.paths)
    first = dash.outputs.reject(oid, "too slow")
    again = dash.outputs.reject(oid, "still too slow")
    assert again["review_note"] == "still too slow" and again["path"] == first["path"]
    assert Path(again["path"]).exists()


def test_reject_with_the_files_already_gone_still_rejects(tmp_home: Path):
    dash = make(tmp_home)
    oid = seed_output(dash.state.db, dash.state.paths, with_files=False)
    assert dash.outputs.reject(oid, "gone")["status"] == "rejected"


def test_an_uploaded_video_cannot_be_rejected_and_keeps_its_files(tmp_home: Path):
    dash = make(tmp_home)
    oid = seed_output(dash.state.db, dash.state.paths, status="uploaded")
    path = Path(dash.outputs.detail(oid)["path"])
    with pytest.raises(ApiError) as ei:
        dash.outputs.reject(oid, "late")
    assert ei.value.code == "already_uploaded" and path.exists()


# ---------------------------------------------------------------- the updater says what went wrong


def test_the_error_line_is_the_error_not_the_generic_hint():
    stderr = (
        "error NS-E118: GitHub did not hand out the update. (github.com: HTTP 403: GitHub's limit of update "
        "checks per hour was reached; try again in an hour)\n"
        "hint: GitHub answered with an error."
    )
    assert error_line(stderr).startswith("NS-E118: GitHub did not hand out the update.")
    assert "hour" in error_line(stderr)
    assert error_line("some plain failure\nsecond line") == "second line"
    assert error_line("") == "The update check failed."
    res = classify_update(parse_update_output(1, "", stderr))
    assert res["state"] == "error" and res["message"].startswith("NS-E118")
    old_hint = "hint: Check the internet connection and any firewall/proxy."
    assert old_hint not in res["message"]


def _status_client(status: int, headers: dict[str, str] | None = None, text: str = "") -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, text=text)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "headers", "text", "words"),
    [
        (403, {"x-ratelimit-remaining": "0"}, "", "limit of update checks"),
        (403, {}, "API rate limit exceeded for 1.2.3.4", "limit of update checks"),
        (429, {}, "", "limit of update checks"),
        (404, {}, "", "no release is published"),
        (503, {}, "", "problem right now"),
        (418, {}, "", "refused the request"),
    ],
)
def test_a_github_error_answer_is_ns_e118_and_says_so(
    status: int, headers: dict[str, str], text: str, words: str
):
    with pytest.raises(NsError) as ei:
        releases.fetch_latest(_status_client(status, headers, text))
    assert ei.value.code == "NS-E118" and f"HTTP {status}" in ei.value.detail and words in ei.value.detail
    assert "internet" not in ei.value.detail.lower()


def test_a_lost_connection_stays_ns_e105():
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(NsError) as ei:
        releases.fetch_latest(httpx.Client(transport=httpx.MockTransport(refuse)))
    assert ei.value.code == "NS-E105"


def test_a_download_that_github_refuses_is_ns_e118(tmp_path: Path):
    rel = releases.Release(
        "0.2.0", "v0.2.0", "", "Nightshift-Update-0.2.0.zip", "https://github.com/x/y/a.zip", "a" * 64, None
    )
    with pytest.raises(NsError) as ei:
        releases.download(rel, tmp_path, _status_client(404))
    assert ei.value.code == "NS-E118" and not list(tmp_path.glob("*.zip"))


# ---------------------------------------------------------------- Settings > From a file


class FileUpdate:
    """Records the zip the dashboard hands to `ns update <zip>`."""

    def __init__(self, state: str = "started") -> None:
        self.files: list[Path] = []
        self.state = state

    def apply_file(self, zip_path: Path) -> dict[str, Any]:
        self.files.append(zip_path)
        message = "NS-E701: SHA256 mismatch" if self.state == "error" else "started"
        return {"state": self.state, "message": message}


def update_dash(tmp_home: Path, state: str = "started") -> tuple[Dashboard, FileUpdate]:
    dash = make(tmp_home)
    fake = FileUpdate(state)
    dash.state.adapters.update = fake  # type: ignore[assignment]
    return dash, fake


ZIP = "Nightshift-Update-0.1.8.zip"


def test_update_from_a_file_saves_both_files_side_by_side_and_calls_ns_update(tmp_home: Path):
    dash, fake = update_dash(tmp_home)
    sidecar = io.BytesIO(b"ab" * 32 + f"  {ZIP}\n".encode())
    res = dash.library.update_apply_file(ZIP, io.BytesIO(b"PK\x03\x04zip"), sidecar)
    assert res["state"] == "started"
    (zip_path,) = fake.files
    assert zip_path.name == ZIP and zip_path.parent == dash.state.paths.data / "updates"
    assert zip_path.read_bytes() == b"PK\x03\x04zip"
    side = zip_path.with_name(ZIP + ".sha256")  # where `ns update` looks for it
    assert side.read_text(encoding="utf-8").startswith("ab" * 32)


OTHER_FILES = ["Nightshift-Setup-0.1.8.zip", "notes.txt", "update.zip", "../Nightshift-Update-0.1.8.exe"]


@pytest.mark.parametrize("name", OTHER_FILES)
def test_update_from_a_file_refuses_other_files(tmp_home: Path, name: str):
    dash, fake = update_dash(tmp_home)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(name, io.BytesIO(b"x"), io.BytesIO(b"y"))
    assert ei.value.code == "bad_file_type" and fake.files == []


def test_update_from_a_file_needs_the_checksum_file(tmp_home: Path):
    dash, fake = update_dash(tmp_home)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"x"), None)
    assert ei.value.code == "no_checksum" and ZIP + ".sha256" in str(ei.value) and fake.files == []
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"x"), io.BytesIO(b"  \n"))
    assert ei.value.code == "empty_file" and fake.files == []


def test_update_from_a_file_passes_the_refusal_of_ns_update_on(tmp_home: Path):
    dash, _fake = update_dash(tmp_home, "error")
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"x"), io.BytesIO(b"c" * 64))
    assert ei.value.status == 409 and "SHA256" in str(ei.value)


def test_update_from_a_file_waits_for_a_running_job(tmp_home: Path):
    dash, fake = update_dash(tmp_home)
    rec = dash.state.store.create("verify", None, {})
    dash.state.store.mark_running(rec.job_id)
    with pytest.raises(ApiError) as ei:
        dash.library.update_apply_file(ZIP, io.BytesIO(b"x"), io.BytesIO(b"c" * 64))
    assert ei.value.code == "job_running" and fake.files == []


def test_the_route_takes_both_files(tmp_home: Path):
    from fastapi.testclient import TestClient

    from ns_dashboard.server import create_app

    dash, fake = update_dash(tmp_home)
    app = create_app(state=dash.state, start_worker=False, static_dir=tmp_home / "absent")
    client = TestClient(app, base_url="http://127.0.0.1:8100")
    r = client.post(
        "/api/update/file",
        files={
            "file": (ZIP, b"PK\x03\x04", "application/zip"),
            "checksum": (ZIP + ".sha256", b"d" * 64, "text/plain"),
        },
    )
    assert r.status_code == 200 and r.json()["state"] == "started" and len(fake.files) == 1
    only_zip = client.post("/api/update/file", files={"file": (ZIP, b"PK", "application/zip")})
    assert only_zip.status_code == 400 and only_zip.json()["error"]["code"] == "no_checksum"
    assert client.post("/api/update/file").status_code == 400


def test_the_real_adapter_runs_ns_update_with_the_zip(tmp_home: Path):
    from ns_dashboard.updater import RealUpdateAdapter

    seen: list[list[str]] = []

    def runner(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        seen.append(cmd)
        return 0, '{"started": true}\n', ""

    zip_path = tmp_home / ZIP
    res = RealUpdateAdapter(Paths.from_env(), runner).apply_file(zip_path)
    assert seen[0][-2:] == ["update", str(zip_path)] and res["state"] == "started"


def test_the_settings_page_installs_from_the_two_files():
    src = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert '"/update/file"' in src and "Installing from the browser is not available yet" not in src
    assert "Nightshift-Update-<version>.zip" in src and ".sha256" in src and "nightshift-app zip" not in src


def test_the_release_notes_say_the_checksum_is_required_for_the_update_zip():
    notes = (ROOT / "dev" / "release_notes.md").read_text(encoding="utf-8")
    lines = notes.splitlines()
    row = next(line for line in lines if line.startswith("| `Nightshift-Update-<ver>.zip.sha256`"))
    assert "Optional" not in row and "required" in row.lower()
    setup_row = next(line for line in lines if line.startswith("| `Nightshift-Setup-<ver>.zip.sha256`"))
    assert "Optional" in setup_row  # the Setup zip's checksum stays optional


# ---------------------------------------------------------------- codes


def test_new_codes_exist_with_hints():
    for code in ("NS-E118", "NS-E411"):
        d = describe(code)
        assert d.kind == "error" and d.message and d.hint, code
    assert "hour" in describe("NS-E118").hint


def test_whisper_that_cannot_load_has_its_own_code_and_the_verify_row_carries_it():
    from ns_supervisor import verify

    src = Path(verify.__file__).read_text(encoding="utf-8")
    assert 'rec.run(names.WHISPER_LOAD, whisper, "NS-E411")' in src
    import json

    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    assert copy["NS-E411"]["title"] and copy["NS-E411"]["whatToDo"]
