"""A frozen copy of the config models of Nightshift 0.1.14 (`ns_common/models.py` at tag v0.1.14).

What `ns update --rollback` leaves behind must load with the models of the version it goes back to, and those
models are strict (`extra="forbid"`): a key a newer version wrote makes the older code refuse its own file.
Tests load the restored config with these definitions, so they fail the way a real rollback to 0.1.14 would.
Only the models that read `config/` are kept (Runtime, InstallState, Channel and what they hold); the text is
the tag's, class for class, apart from the imports this slice does not use and the spacing of comments
(ruff format). Do not edit it: a newer copy of the same kind (frozen 0.1.15) goes in a new file.
"""

from __future__ import annotations

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
    name: str | None = None  # library name for design/clone voices
    speed: float = Field(default=1.0, ge=0.7, le=1.3)


# Per-channel caption options (spec 17.2). A comment, not a docstring: pydantic would put it in the schema.
class CaptionsOpts(Strict):
    mask_profanity: bool = False  # mask listed words in the captions; the audio is untouched
    remove_fillers: bool = False  # clips: leave um / uh / er out of the captions (the audio is untouched)


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
    qc_pass_target: float = Field(default=0.9, ge=0, le=1)  # QC pass-rate line (CR-C9-1)
    pools: Pools = Pools()
    avoid_topics: list[str] = []
    formats_allowed: list[str] = []  # format preset ids (spec 17.1); empty = every shipped preset
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
    story_model_path: str | None = None  # two-model mode only
    aliases: list[str] = ["code", "story"]
    flags: LlamaFlags = LlamaFlags()
    story_flags: LlamaFlags | None = None  # two-model mode: flags for the story model (None = use `flags`)
    build: Literal["cuda13", "cuda12"] | None = None  # llama.cpp pair chosen by the installer

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
    # qwentts: qwentts.cpp; reference: the PyTorch engine (installer fallback, NS-W301); faster: legacy name
    # for qwentts; fake: the tone-burst engine of an engine test (answer `fake_voice`, docs/DEV_MODE.md).
    engine: Literal["qwentts", "reference", "faster", "fake"] = "qwentts"
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
    # Dev install (`INSTALL.cmd --dev`, docs/DEV_MODE.md): fake engines, stub model files. Consumers read it
    # through `ns_common.devmode`; a real install never sets it.
    dev_mode: bool = False
    # Engine test (answer `fake_footage`, docs/DEV_MODE.md): the fake clip source stands in for Pexels and
    # Pixabay so story jobs run without a stock-footage account. A real install never sets it except
    # through that answer.
    fake_footage: bool = False

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
    dev_mode: bool = False  # installed with `INSTALL.cmd --dev` (docs/DEV_MODE.md)
