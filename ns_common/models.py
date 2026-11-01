from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
JOB_ID_RE = r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-[0-9a-f]{4}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- channel ----------
class Voice(Strict):
    mode: Literal["preset", "design", "clone"] = "preset"
    speaker: str | None = "Ryan"
    description: str | None = None
    name: str | None = None          # library name for design/clone voices
    speed: float = Field(default=1.0, ge=0.7, le=1.3)


class Pools(Strict):
    hooks: list[str] = ["cold_open", "question", "confession", "warning"]
    structures: list[str] = ["twist", "reverse_reveal", "slow_burn", "unreliable_narrator"]
    pov_tones: list[str] = ["first_person_intimate", "narrator_calm"]
    pacings: list[str] = ["standard", "measured"]
    caption_styles: list[str] = ["punch_yellow", "clean_white"]
    visual_treatments: list[str] = ["dark_slowzoom", "handheld_grit"]
    title_formulas: list[str] = ["question", "statement_twist", "i_never"]


class Channel(Strict):
    slug: str = Field(pattern=SLUG_RE)
    display_name: str
    status: Literal["active", "paused"] = "active"
    language: str = "en"
    niche: str
    seed_keywords: list[str] = []
    voice_notes: str = ""
    voice: Voice = Voice()
    format: Literal["master_first", "shorts_only", "series_compilation"] = "master_first"
    masters_per_week: int = Field(default=1, ge=1, le=3)
    long_minutes: tuple[int, int] = (10, 14)
    shorts_per_master: tuple[int, int] = (4, 6)
    shorts_per_day_max: int = Field(default=2, ge=1, le=3)
    length_buckets: list[int] = [35, 50, 75]
    publish_window: str = Field(default="18:00-21:00", pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    audience: Literal["general", "mature"] = "general"
    made_for_kids: bool = False
    fiction_label: bool = True
    creator_note: str = ""
    critic_bar: float = Field(default=8.0, ge=5.0, le=10.0)
    pools: Pools = Pools()
    avoid_topics: list[str] = []
    created_at: str | None = None

    @field_validator("long_minutes")
    @classmethod
    def _floor(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] < 9 or v[1] < v[0]:
            raise ValueError("long_minutes must be [>=9, >=min]")
        return v


# ---------- runtime ----------
class LlamaFlags(Strict):
    n_gpu_layers: int = 99
    n_cpu_moe: int = 0
    ctx: int = 16384
    kv_type: Literal["f16", "q8_0"] = "f16"
    spec_type: Literal["draft-mtp"] | None = None
    spec_draft_n_max: int | None = None
    threads: int | None = None


class LlamaCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8080
    model_path: str = ""
    story_model_path: str | None = None     # two-model mode only
    aliases: list[str] = ["code", "story"]
    flags: LlamaFlags = LlamaFlags()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class EmbedCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8081
    model_path: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TtsCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8091
    engine: Literal["qwentts", "reference", "faster"] = "qwentts"
    model_dir: str = ""
    device: Literal["cuda", "cpu"] = "cuda"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class WhisperCfg(Strict):
    model: str = "small.en"
    device: Literal["cuda", "cpu"] = "cuda"
    compute_type: str = "float16"


class VideoCfg(Strict):
    encoder: Literal["h264_nvenc", "libx264"] = "h264_nvenc"
    preset: str = "p4"
    fps: int = 30


class Runtime(Strict):
    profile: Literal["gpu", "cpu-dev"] = "gpu"
    llama: LlamaCfg = LlamaCfg()
    embed: EmbedCfg = EmbedCfg()
    tts: TtsCfg = TtsCfg()
    whisper: WhisperCfg = WhisperCfg()
    video: VideoCfg = VideoCfg()
    supervisor_port: int = 8099

    @classmethod
    def default(cls, profile: Literal["gpu", "cpu-dev"]) -> Runtime:
        if profile == "gpu":
            return cls(profile=profile)
        return cls(
            profile=profile,
            llama=LlamaCfg(flags=LlamaFlags(n_gpu_layers=0, ctx=8192, threads=4)),
            tts=TtsCfg(engine="reference", device="cpu"),
            whisper=WhisperCfg(model="tiny", device="cpu", compute_type="int8"),
            video=VideoCfg(encoder="libx264", preset="ultrafast"),
        )


# ---------- install ----------
class StepState(Strict):
    status: Literal["pending", "done", "failed", "skipped"] = "pending"
    error: str | None = None
    finished_at: str | None = None


class InstallState(Strict):
    version: str = "0.1.0"
    app_path: str = ""
    profile: Literal["gpu", "cpu-dev"] | None = None
    steps: dict[str, StepState] = {}
    started_at: str | None = None
    completed_at: str | None = None


class AnswersChannel(Strict):
    display_name: str
    niche: str
    language: str = "en"
    seed_keywords: list[str] = []


class Answers(Strict):
    channel: AnswersChannel
    voice: Voice = Voice()
    voice_ref_audio_path: str | None = None
    voice_ref_text: str | None = None
    voice_rights_confirmed: bool = False
    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None
    masters_per_week: int = 1
    run_time: str = Field(default="03:00", pattern=r"^\d{2}:\d{2}$")
    install_code: bool = True
    allow_cpu: bool = False


# ---------- manifest ----------
class HfFile(Strict):
    repo: str
    file: str
    sha256: str | None = None
    bytes: int | None = None


class HfRepo(Strict):
    repo: str
    revision: str = "main"
    files_sha256: dict[str, str] = {}


class BinPin(Strict):
    version: str
    url: str
    sha256: str | None = None


class ProfileModels(Strict):
    llm: HfFile
    llm_balanced: HfFile | None = None     # opt-in tier (spec 21.3)
    llm_best: HfFile | None = None         # opt-in tier
    story_llm: HfFile | None = None
    embed: HfFile
    tts: list[HfFile]                      # always-installed qwentts.cpp GGUFs (talkers + tokenizer)
    tts_optional: list[HfFile] = []        # downloaded on demand (VoiceDesign)
    whisper_model: str
    whisper_repo: HfRepo                   # CTranslate2 repo for whisper_model


class Manifest(Strict):
    version: str
    profiles: dict[str, ProfileModels]
    bins: dict[str, BinPin | None]         # None = no usable upstream binary (qwentts)
    python_version: str = "3.12"
    spacy_model: str = "en_core_web_md"
    tts_packages: list[str]     # PyTorch fallback set: exact pip specs for the fallback tts venv


# ---------- text stages ----------
class Idea(Strict):
    premise: str
    twist: str
    setting: str
    protagonist: str
    why_chapters: str = ""
    keywords: list[str] = Field(min_length=1, max_length=8)


class Part(Strict):
    n: int = Field(ge=1)
    text: str
    hook: str = ""
    word_count: int = Field(ge=1)
    est_seconds: float = Field(gt=0)
    cliffhanger: bool = False


class Seo(Strict):
    title: str = Field(max_length=100)
    description: str = Field(max_length=5000)
    tags: list[str] = Field(min_length=1, max_length=20)
    hashtags: list[str] = Field(max_length=5)
    keywords: list[str] = []


class VisualBeat(Strict):
    part: int | None = None
    chapter: int | None = None
    beat: int = Field(ge=0)
    sentence_start: int = Field(ge=0)
    sentence_end: int = Field(ge=0)
    keywords: list[str] = Field(min_length=1, max_length=3)
    mood: str
    need_vertical: bool = True


class Combo(Strict):
    combo_id: str
    hook: str
    structure: str
    pov_tone: str
    pacing: str
    caption_style: str
    visual_treatment: str
    title_formula: str
    length_bucket: int | None = None


# ---------- QC / policy / metadata ----------
class QcCheck(Strict):
    passed: bool
    value: float | str | None = None
    limit: str | None = None


class QcResult(Strict):
    item: str                      # "long" | "s<k>"
    passed: bool
    checks: dict[str, QcCheck]
    reason: str | None = None


class PolicyCheck(Strict):
    passed: bool
    detail: str = ""


class PolicyAdvice(Strict):
    ai_disclosure: Literal["yes", "no"] = "no"
    made_for_kids: bool = False
    age_restrict_suggested: bool = False


class PolicyResult(Strict):
    stage: Literal["text", "final"]
    passed: bool
    checks: dict[str, PolicyCheck]
    advice: PolicyAdvice = PolicyAdvice()


class ClipRef(Strict):
    source: Literal["pexels", "pixabay", "fake"]
    id: str
    url: str
    license: str


class Metadata(Strict):
    channel: str = Field(pattern=SLUG_RE)
    job_id: str = Field(pattern=JOB_ID_RE)
    kind: Literal["long", "short"]
    k: int | None = None
    title: str = Field(max_length=100)
    description: str
    tags: list[str]
    hashtags: list[str]
    publish_after: str
    chapters: list[dict] | None = None
    midroll_suggestions_s: list[float] | None = None
    thumbnails: list[str] | None = None
    related_video_instruction: str | None = None
    parent_job_id: str | None = None
    ai_disclosure: Literal["yes", "no"]
    made_for_kids: bool
    age_restrict_suggested: bool
    clips: list[ClipRef]
    combo_id: str
    prompt_versions: dict[str, int]


class CalibrationRow(Strict):
    story_id: str
    rubric: dict[str, float]
    critic_avg: float
    human_avg: float | None = None


class GateCheck(Strict):
    name: str
    passed: bool
    value: float | str | None = None
    limit: str | None = None
    log: str | None = None


class GateReport(Strict):
    slice: str
    date: str
    machine: dict[str, str | float | None]
    checks: list[GateCheck]
    benchmarks: dict[str, float] = {}

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "channel": Channel, "runtime": Runtime, "install_state": InstallState, "answers": Answers,
    "manifest": Manifest, "idea": Idea, "parts": Part, "seo": Seo, "visual_plan": VisualBeat,
    "combo": Combo, "qc": QcResult, "policy": PolicyResult, "metadata": Metadata,
    "calibration_row": CalibrationRow, "gate_report": GateReport,
}


def export_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMA_MODELS.items():
        f = out_dir / f"{name}.schema.json"
        text = json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"
        f.write_text(text, encoding="utf-8")
        written.append(f)
    return written


if __name__ == "__main__":
    from ns_common.paths import contracts_dir

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else contracts_dir() / "schemas"
    for p in export_schemas(target):
        print(p)
