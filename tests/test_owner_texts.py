# ruff: noqa: E501
"""FN-4: owner texts that were false in some state, pinned to the code they describe.

- T3, the key dialog of Settings: a key is saved in Windows Credential Manager AND in `config\\secrets.dpapi`
  (`ns_dashboard.adapters.RealKeyringAdapter.set`); the dialog said "never in a file".
- The pages' sentences about "tonight's run" are chosen by whether a nightly run is coming
  (`tests/test_nightly_promise_is_conditional.py` guards the rule; this file pins the two sentences of each).
"""

from __future__ import annotations

import base64
import types
from pathlib import Path

from ns_common.paths import Paths
from ns_common.secret_file import FILE_NAME, secret_path, stored_names
from ns_common.secrets_names import KEYRING_SERVICE, PIXABAY_KEY
from ns_dashboard.adapters import RealKeyringAdapter

UI = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"
KEY_DIALOG = (
    "Paste the free key from your account page. It is saved in Windows Credential Manager and, for nightly "
    "runs that cannot reach Credential Manager, in an encrypted copy in the Nightshift folder "
    "(config\\secrets.dpapi); on a drive that keeps permissions only your Windows account and Windows itself "
    "can open that copy"
)


def page(name: str) -> str:
    return (UI / "pages" / name).read_text(encoding="utf-8")


def test_the_key_dialog_names_both_places(tmp_path: Path) -> None:
    source = page("Settings.tsx")
    assert "never in a file" not in source
    assert f'body: "{KEY_DIALOG.replace(chr(92), chr(92) * 2)}"' in source
    assert f"config\\{FILE_NAME}" in KEY_DIALOG
    # the sentence is what a save does: both places get the key
    vault: dict[tuple[str, str], str] = {}
    keyring = types.SimpleNamespace(
        set_password=lambda service, name, value: vault.__setitem__((service, name), value),
        get_password=lambda service, name: vault.get((service, name)),
        delete_password=lambda service, name: vault.pop((service, name)),
    )
    paths = Paths(tmp_path / "ns")
    paths.config.mkdir(parents=True)
    adapter = RealKeyringAdapter(paths, keyring_module=keyring, protect=lambda b: b[::-1], unprotect=lambda b: b[::-1])
    adapter.set(PIXABAY_KEY, "abc")
    assert vault == {(KEYRING_SERVICE, PIXABAY_KEY): "abc"}
    assert stored_names(paths) == {PIXABAY_KEY} and secret_path(paths) == paths.config / FILE_NAME
    assert base64.b64decode(__import__("json").loads(secret_path(paths).read_text(encoding="utf-8"))[PIXABAY_KEY]) == b"cba"


def test_the_queue_says_what_continues_a_waiting_job_in_each_state() -> None:
    source = page("Queue.tsx")
    # a nightly run is coming: it continues the job. None is coming: the reason, and the way that works now
    assert "waiting for the GPU: the next night continues it" in source
    assert "no channel is active, so no nightly run is coming" in source and '"Automatic runs are off"' in source
    assert "press Retry from where it stopped on its card in Create, under Earlier" in source
    assert "continue it with studio run --resume ${u.job_id} in the Nightshift Studio window" in source
    # the place the sentence names: the failed cards of Create's "Earlier" list carry that button
    create = page("Create.tsx")
    assert 'title="Earlier"' in create and "{failed && <JobError j={j} />}" in create
    assert '"Retry from where it stopped"' in (UI / "ui" / "JobError.tsx").read_text(encoding="utf-8")
    # the banner of a held graphics card, and the empty Ready list
    assert "Tonight's run waits for it." in source
    assert "Jobs that need it wait." in source
    assert "after tonight's run" in source and "when you make them" in source


def test_results_and_the_assistant_promise_no_night() -> None:
    assert "tonight's run" not in page("Results.tsx") and "Applied to the next video made" in page("Results.tsx")
    assistant = page("Assistant.tsx")
    assert "tonight's run" not in assistant
    assert "A paused channel makes no story videos until it is resumed" in assistant
