"""C8 Clipper: `studio clip` command line."""
from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ns_studio.clipper import cli
from ns_studio.context import FAKEABLE

runner = CliRunner()


def test_parse_fake() -> None:
    assert cli.parse_fake(None) == frozenset()
    assert cli.parse_fake("all") == FAKEABLE
    assert cli.parse_fake("llm, whisper") == frozenset({"llm", "whisper"})
    with pytest.raises(typer.BadParameter):
        cli.parse_fake("llm,nope")


def test_help_lists_the_options() -> None:
    res = runner.invoke(cli.app, ["--help"])
    assert res.exit_code == 0
    for opt in ("--channel", "--template", "--max-clips", "--min-s", "--max-s"):
        assert opt in res.output
    for opt in ("--fake-services", "--reselect"):
        assert opt in res.output


def test_needs_a_video_and_channel() -> None:
    res = runner.invoke(cli.app, ["--confirm-rights"])
    assert res.exit_code == 2


def test_declining_the_rights_prompt_stops(tmp_home: Path) -> None:
    res = runner.invoke(cli.app, ["movie.mp4", "--channel", "after-dark"], input="n\n")
    assert res.exit_code == 1
    assert "Rights not confirmed" in res.output


def test_unknown_channel_is_a_clean_error(tmp_home: Path) -> None:
    res = runner.invoke(cli.app, ["movie.mp4", "--channel", "nope", "--confirm-rights"])
    assert res.exit_code == 1
    assert "not found" in res.output


def test_bare_fake_services_flag_is_accepted(tmp_home: Path) -> None:
    res = runner.invoke(cli.app, ["movie.mp4", "--fake-services", "--channel", "nope", "--confirm-rights"])
    assert res.exit_code == 1  # reaches the channel lookup, so the flag parsed
    assert "not found" in res.output


def test_bad_template_is_rejected(tmp_home: Path) -> None:
    from ns_common.config import save_channel
    from ns_common.paths import Paths
    from tests.helpers import make_channel

    paths = Paths.from_env()
    paths.ensure()
    save_channel(paths, make_channel())
    res = runner.invoke(
        cli.app,
        ["movie.mp4", "--channel", "after-dark", "--template", "nope", "--confirm-rights"],
    )
    assert res.exit_code == 2
