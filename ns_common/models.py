from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# Per-channel caption options (spec 17.2). A comment, not a docstring: pydantic would put it in the schema.
class CaptionsOpts(Strict):
    mask_profanity: bool = False     # mask listed words in the captions; the audio is untouched
    remove_fillers: bool = False     # clips: leave um / uh / er out of the captions (the audio is untouched)


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
    qc_pass_target: float = Field(default=0.9, ge=0, le=1)   # QC pass-rate line (CR-C9-1)
    pools: Pools = Pools()
    avoid_topics: list[str] = []
    formats_allowed: list[str] = []     # format preset ids (spec 17.1); empty = every shipped preset
    captions: CaptionsOpts = CaptionsOpts()
    created_at: str | None = None

    @field_validator("long_minutes")
    @classmethod
    def _floor(cls, v: tuple[int, int]) -> tuple[int, int]:
        if v[0] < 9 or v[1] < v[0] or v[1] > 14:
            raise ValueError("long_minutes must be [>=9, >=min, <=14] (QC fails a master over 15:00)")
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
    story_flags: LlamaFlags | None = None     # two-model mode: flags for the story model (None = use `flags`)
    build: Literal["cuda13", "cuda12"] | None = None   # llama.cpp pair chosen by the installer

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class EmbedCfg(Strict):
    host: str = "127.0.0.1"
    port: int = 8081
    model_path: str = ""
    # llama-server's default physical batch (-ub 512) rejects any input longer than 512 tokens; the embedding
    # model's own context is 2048. The supervisor passes these as -c, -b and -ub (see `embed_server_args`).
    ctx_size: int = 2048
    batch_size: int = 2048

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
    model: str = "distil-large-v3"
    device: Literal["cuda", "cpu"] = "cuda"
    compute_type: str = "int8"


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


# A story model the owner named in the setup window (CR-C1-UI-9): a Hugging Face GGUF and its resolved size.
# It has no pinned SHA256; the installer allows that only for this user-supplied model (NS-W113).
# (No docstring on purpose: pydantic would copy it into the exported schema as a description.)
class LlmCustom(Strict):
    repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    file: str = Field(min_length=6, max_length=240)
    size: int = Field(gt=0)


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
    # installer-only keys for unattended installs (spec 5 and 1.3)
    home_path: str | None = None
    models_source: str | None = None
    llm_tier: Literal["small", "balanced", "best"] = "small"
    llm_custom: LlmCustom | None = None    # CR-C1-UI-9: overrides the tier's model when set
    restore_from: str | None = None
    # installer UI (CR-C1-UI-1): greeting name, default dashboard theme, where the models come from
    owner_name: str | None = Field(default=None, max_length=40)
    ui_theme: Literal["light", "dark", "soft"] = "dark"
    models_source_kind: Literal["internet", "folder", "drive_zip"] = "internet"


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
    optional: bool = False           # only optional pins may carry sha256 "TBD-release" (CR-C1-UI-3)


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


class PythonPins(Strict):
    # Hash-locked pip pins for the studio venv (CR-C1-4 a and b).
    # Requirements-file lines `name==version --hash=sha256:<hex>`: the cuBLAS and cuDNN DLLs that
    # CTranslate2 (faster-whisper) needs on Windows.
    whisper_cuda: list[str] = []
    # The spaCy model wheel (a GitHub release asset, not on PyPI): version, url and the sha256 of the wheel.
    spacy_wheel: BinPin | None = None


class Manifest(Strict):
    version: str
    profiles: dict[str, ProfileModels]
    bins: dict[str, BinPin | None]         # None = no usable upstream binary (qwentts)
    python_version: str = "3.12"
    spacy_model: str = "en_core_web_md"
    tts_packages: list[str]     # PyTorch fallback set: exact pip specs for the fallback tts venv
    python: PythonPins = PythonPins()
    # Hugging Face repos of the PyTorch reference TTS engine (the fallback), downloaded as safetensors.
    tts_fallback_repos: list[str] = [
        "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    ]


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
    emphasis: dict[str, str] | None = None     # sentence index (as a JSON key) -> one key word (spec 17.2)


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
    kind: Literal["long", "short", "clip"]
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
    source_video: str | None = None      # set for clips (spec 15.6)
    template: str | None = None          # clip layout template used


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


# ---------- master-first text artifacts (spec 15.6; CR-C5-1) ----------
# Chapter plan of the long form (`plan.json`): 5-7 chapters, each with beats, a cliff and a word budget.
class PlanChapter(Strict):
    n: int = Field(ge=1)
    title: str
    beats: list[str] = Field(min_length=1)
    cliff: str
    word_budget: int = Field(ge=1)


class Plan(Strict):
    target_minutes: int = Field(ge=9)
    target_words: int = Field(ge=1)
    chapters: list[PlanChapter] = Field(min_length=5, max_length=7)


# A sentence of a chapter with its 0-based index (the unit Shorts selection and beats address).
class Sentence(Strict):
    idx: int = Field(ge=0)
    text: str


class StoryChapter(Strict):
    n: int = Field(ge=1)
    title: str
    text: str
    summary: str
    word_count: int = Field(ge=1)
    est_seconds: float = Field(gt=0)
    cliff_line_idx: int | None = Field(default=None, ge=0)
    sentences: list[Sentence] = Field(min_length=1)


# `story.json`: the master story by chapter.
class Story(Strict):
    working_title: str
    chapters: list[StoryChapter] = Field(min_length=1)
    revisions: int = Field(default=0, ge=0)


# One derived Short: a sentence span of one chapter plus hook candidates (1..5: hooks over 12 words
# are dropped in code) and the closing line. `shorts_plan.json` is the bare list of these.
class ShortsPlanItem(Strict):
    k: int = Field(ge=1)
    chapter: int = Field(ge=1)
    sentence_start: int = Field(ge=0)
    sentence_end: int = Field(ge=0)
    start_s: float | None = None
    end_s: float | None = None
    hooks: list[str] = Field(min_length=1, max_length=5)
    chosen_hook: str
    closing_line: str
    length_bucket: int = Field(ge=1)
    combo_id: str
    why_standalone: str


# Wrapper form of the plan, for prompts that answer {"shorts": [...]}.
class ShortsPlan(Strict):
    shorts: list[ShortsPlanItem] = Field(min_length=1)


class ChapterTitle(Strict):
    n: int = Field(ge=1)
    title: str


# `seo_long.json`: `Seo` plus the chapter titles (timestamps are filled in after rendering).
class SeoLong(Seo):
    chapter_titles: list[ChapterTitle] = Field(min_length=1)


# One entry of `clip_plan.json` (the bare list). `clip_select` fills layout, layout_reason and
# caption_style; status/reasons record why a candidate was dropped by the judge or the filters.
class ClipPlanItem(Strict):
    k: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    title: str = Field(max_length=100)
    hook_line: str | None = None
    why: str = ""
    score: float = Field(ge=0, le=1)
    speakers: list[int] | None = None
    keywords: list[str] = []
    layout: str | None = None            # format preset id chosen by the model
    layout_reason: str | None = None
    caption_style: str | None = None     # model-chosen caption style (spec 17.2); None = channel default
    emphasis: list[str] | None = None   # one key word per sentence in order, "" skips a sentence (spec 17.2)
    status: Literal["kept", "dropped"] = "kept"
    reasons: list[str] = []

    @model_validator(mode="after")
    def _ordered(self) -> ClipPlanItem:
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


# Wrapper form of the plan: the `clip_select` prompt answers {"clips": [...]}.
class ClipPlan(Strict):
    clips: list[ClipPlanItem] = Field(min_length=1)


# Answer of `policy_check.md`; `unsafe_categories` holds ids of contracts/policy/unsafe_topics.yaml.
class PolicyCheckResult(Strict):
    unsafe_categories: list[str]
    hook_honest: bool
    title_honest: bool
    standalone: bool | None
    notes: str
    tags_relevant: bool | None = None    # only when tags were given: true when every tag describes the story


# ---------- clipper (spec 15.6; CR-C8-1, CR-C8-6) ----------
CANVAS_W = 1080
CANVAS_H = 1920


# Axis-aligned rectangle in pixels of the frame it refers to.
class Rect(Strict):
    x: int
    y: int
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0

    def inside(self, outer: Rect, tol: int = 0) -> bool:
        return (
            self.x >= outer.x - tol
            and self.y >= outer.y - tol
            and self.right <= outer.right + tol
            and self.bottom <= outer.bottom + tol
        )


class Segment(Strict):
    i: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    speaker: int | None = None


# One timed word; `w` keeps the transcript's spelling and punctuation.
class Word(Strict):
    w: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    seg: int = Field(ge=0)


class Transcript(Strict):
    source: str
    duration_s: float = Field(gt=0)
    language: str = "en"
    segments: list[Segment]
    words: list[Word]

    @model_validator(mode="after")
    def _ordered(self) -> Transcript:
        for s in self.segments:
            if s.end < s.start:
                raise ValueError(f"segment {s.i} ends before it starts")
        for w in self.words:
            if w.end < w.start:
                raise ValueError(f"word {w.w!r} ends before it starts")
        return self


# Face boxes seen at time `t`; `speaking` indexes `boxes` (sorted left to right).
class FaceFrame(Strict):
    t: float = Field(ge=0)
    boxes: list[Rect]
    speaking: int | None = None


# Source region(s) shown during [t_start, t_end] (source seconds): `region` is the main panel,
# `region_b` the second panel (camera, or the lower speaker), `speaking` 0 top / 1 bottom.
class Crop(Strict):
    t_start: float = Field(ge=0)
    t_end: float = Field(ge=0)
    region: Rect
    region_b: Rect | None = None
    speaking: int | None = None


# `clip_layout.json`; `note` records fallbacks such as NS-W402, `per_clip` maps clip k to its template.
class ClipLayout(Strict):
    template: str
    facecam: Rect | None = None
    faces: list[FaceFrame] = []
    crops: list[Crop] = []
    note: str | None = None
    per_clip: dict[str, str] = {}
    notes: dict[str, str] = {}


# A panel of the 1080x1920 canvas; `y="seam"` and `h="auto"|"rest"` are resolved from the camera.
class Region(Strict):
    id: str
    x: int = 0
    y: int | Literal["seam"] = 0
    w: int = CANVAS_W
    h: int | Literal["auto", "rest"] = "rest"
    max_h: int | None = None


# One entry of presets/clip_templates.yaml.
class ClipTemplate(Strict):
    name: str
    layout: Literal["gaming", "stacked", "tracked", "center", "screen_cam"]
    description: str
    needs: Literal["none", "facecam", "faces_1", "faces_2"] = "none"
    when: str = ""
    regions: list[Region]
    caption_anchor_y_pct: float | Literal["seam"]
    safe_top: int = 220
    safe_bottom: int = 320
    title_card: bool = False
    title_card_s: float = 1.2
    speaker_border_px: int = 0
    track_deadband: float = 0.2
    track_ramp_s: float = 1.0


# ---------- format presets (spec 17.1; presets/formats/<id>.yaml) ----------
class FormatCanvas(Strict):
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    fps: int = Field(default=30, ge=1)


class FormatDuration(Strict):
    min_s: float = Field(ge=0)
    max_s: float = Field(gt=0)
    buckets: list[int] | None = None


class FormatCaptions(Strict):
    styles_allowed: list[str] = []
    anchor_y_pct: float = Field(default=70.0, ge=0, le=100)
    words_on_screen: int = Field(default=4, ge=1)


# One panel of the output frame. `source`: full | facecam | face:<n> | screen | blur_fill.
class FormatRegion(Strict):
    name: str
    x: int = Field(default=0, ge=0)
    y: int = Field(default=0, ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    source: str = Field(default="full", pattern=r"^(full|facecam|screen|blur_fill|face:\d+)$")


class FormatLayout(Strict):
    regions: list[FormatRegion] = Field(min_length=1)
    seam_captions: bool = False


class FormatMargins(Strict):
    top: int = Field(default=0, ge=0)
    bottom: int = Field(default=0, ge=0)


class FormatShots(Strict):
    min_s: float = Field(gt=0)
    max_s: float = Field(gt=0)


class FormatEncoder(Strict):
    preset: str = "p4"


class FormatSheet(Strict):
    hashtags_required: list[str] = []
    description_rules: str = ""


class Format(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    kind: Literal["short", "long", "square", "clip"]
    canvas: FormatCanvas
    duration: FormatDuration
    captions: FormatCaptions
    layout: FormatLayout
    safe_margins: FormatMargins = FormatMargins()
    shots: FormatShots | None = None
    encoder: FormatEncoder | None = None
    sheet: FormatSheet | None = None


# ---------- dashboard API contracts (CR-C3-UI-*) ----------
# Shapes of what `ns_dashboard` sends and receives. Comments, not docstrings: pydantic would put a
# docstring in the generated schema.


class GpuStatus(Strict):
    name: str
    load_pct: float | None = None        # 0..100, None when the driver does not report it
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None
    temp_c: float | None = None


class StatusEngine(Strict):
    state: Literal["idle", "starting", "loading", "ready", "working", "error"] = "idle"
    detail: str | None = None
    elapsed_s: int | None = None
    vram_owner: str | None = None
    reachable: bool = True
    service: str | None = None
    draining: bool = False
    progress: float | None = None
    step: int | None = None
    steps: int | None = None
    vram_gb: float | None = None         # kept for the status strip; `Status.gpu` has the live numbers


class StatusService(Strict):
    state: str = "stopped"
    vram_mb: int | None = None


class StatusDisk(Strict):
    free_gb: float | None = None
    total_gb: float | None = None
    home: str = ""


class StatusChannel(Strict):
    slug: str
    display_name: str
    status: str
    niche: str | None = None
    voice: str | None = None
    masters_per_week: int | None = None
    publish_window: str | None = None
    format: str | None = None
    pending: int = 0
    tonight: str | None = None


class StatusLastError(Strict):
    job_id: str
    message: str | None = None
    kind: str | None = None


# `GET /api/status` and every `status` event on `GET /api/events`.
class Status(Strict):
    version: str | None = None
    engine: StatusEngine
    next_run: str | None = None
    next_run_in_s: int | None = None
    schedule_time: str | None = None
    schedule_registered: bool | None = None  # False: Windows has no 'Nightshift Daily' task (NS-W206)
    # interactive_only: Windows refused the unattended mode (NS-W205); it runs only while signed in
    schedule_mode: Literal["registered", "interactive_only", "none"] | None = None
    outbox: dict[str, int] = {}
    channels: list[StatusChannel] = []
    services: dict[str, StatusService] = {}
    disk: StatusDisk | None = None
    job: dict[str, Any] | None = None
    queue: dict[str, int] = {}
    last_run: dict[str, Any] | None = None
    incidents: list[dict[str, Any]] = []
    last_error: StatusLastError | None = None
    gpu: GpuStatus | None = None         # optional live GPU readout (CR-C3-UI-4)


# `POST /api/jobs/run`: queue a story for one channel (Create wizard, "When" step).
class JobRunRequest(Strict):
    channel: str
    format: str | None = None            # None = the channel's own format
    count: int | None = Field(default=None, ge=1, le=20)
    schedule_at: datetime | None = None  # null = run now; a naive time is the PC's local time (CR-C3-UI-5)


# `POST /api/jobs/clip` (JSON body; the multipart variant sends the same fields as text plus a `file` part).
# One video source is expected: `url`, `path`, `upload_id`, `source_id` (re-run) or the multipart file.
class JobClipRequest(Strict):
    channel: str
    rights_confirmed: Literal[True]       # the owner owns the video or is licensed to use it (spec 19.1)
    url: str | None = None
    path: str | None = None
    upload_id: str | None = None
    source_id: str | None = None
    template: str = "auto"                # a clip layout / format preset id, "auto" = the model chooses
    max_clips: int = Field(default=6, ge=1, le=10)
    min_s: int = Field(default=20, ge=5, le=120)
    max_s: int = Field(default=60, ge=10, le=180)
    caption_style: str | None = None
    schedule_at: datetime | None = None   # null = run now; a naive time is the PC's local time (CR-C3-UI-5)

    @model_validator(mode="after")
    def _lengths(self) -> JobClipRequest:
        if self.max_s < self.min_s:
            raise ValueError("max_s must not be smaller than min_s")
        return self


# The `stats` object of a channel in `GET /api/channels` (Channels page, Home cards).
class ChannelStats(Strict):
    week: int | None = None
    watched_pct: float | None = None
    spark: list[float] = []
    qc_pass_rate: float | None = Field(default=None, ge=0, le=1)   # share of videos that passed QC, 0..1
    qc_pass_target: float = Field(default=0.9, ge=0, le=1)          # the line the page draws (CR-C3-UI-6)


# `POST /api/voices/design` (VoiceDesign) and `POST /api/voices/clone` (Base model), CR-C3-UI-7.
class VoiceDesignRequest(Strict):
    name: str = Field(min_length=1)          # library name of the new voice
    description: str = Field(min_length=1)   # plain-English description of the voice
    test_line: str = Field(min_length=1)     # sentence spoken for the preview


class VoiceCloneRequest(Strict):
    name: str = Field(min_length=1)
    sample_path: str = Field(min_length=1)   # recording of the voice, already on this PC
    consent: Literal[True]                   # the owner confirmed they may clone this voice
    test_line: str = Field(min_length=1)


class VoiceMakeResponse(Strict):
    voice_id: str
    preview_wav_path: str


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "channel": Channel, "runtime": Runtime, "install_state": InstallState, "answers": Answers,
    "manifest": Manifest, "idea": Idea, "parts": Part, "seo": Seo, "visual_plan": VisualBeat,
    "combo": Combo, "qc": QcResult, "policy": PolicyResult, "metadata": Metadata,
    "calibration_row": CalibrationRow, "gate_report": GateReport,
    "plan": Plan, "story": Story, "shorts_plan": ShortsPlanItem, "seo_long": SeoLong,
    "clip_plan": ClipPlanItem, "policy_check": PolicyCheckResult, "transcript": Transcript,
    "clip_layout": ClipLayout, "clip_template": ClipTemplate, "format": Format,
    "status": Status, "job_run_request": JobRunRequest, "job_clip_request": JobClipRequest,
    "channel_stats": ChannelStats, "voice_design_request": VoiceDesignRequest,
    "voice_clone_request": VoiceCloneRequest, "voice_make_response": VoiceMakeResponse,
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
