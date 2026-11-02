"""The ordered list of wizard steps (component C1, spec 5 table)."""

from __future__ import annotations

from installer.stage2.base import Step
from installer.stage2.steps.s00_welcome import S00Welcome
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s02_download_bins import S02DownloadBins
from installer.stage2.steps.s03_download_models import S03DownloadModels
from installer.stage2.steps.s04_venvs import S04Venvs
from installer.stage2.steps.s05_opencode_prewarm import S05OpencodePrewarm
from installer.stage2.steps.s06_tune import S06Tune
from installer.stage2.steps.s07_configs import S07Configs
from installer.stage2.steps.s08_smoke import S08Smoke
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.steps.v01_samples import V01Samples


def default_steps() -> list[Step]:
    """Fresh instances of every step, s00 to s10 (the console wizard and the unattended path)."""
    return [
        S00Welcome(), S01Questions(), S02DownloadBins(), S03DownloadModels(), S04Venvs(),
        S05OpencodePrewarm(), S06Tune(), S07Configs(), S08Smoke(), S09ShortcutsScheduler(), S10Summary(),
    ]


def bridge_steps() -> list[Step]:
    """The steps for the setup window: no console welcome (the window has its own), plus the samples step."""
    return [
        S01Questions(), S02DownloadBins(), S03DownloadModels(), S04Venvs(), S05OpencodePrewarm(), S06Tune(),
        S07Configs(), S08Smoke(), V01Samples(), S09ShortcutsScheduler(), S10Summary(),
    ]
