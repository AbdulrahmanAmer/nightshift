# ruff: noqa: E501
"""Re-review row 1: the embed window is sized by tokens, not characters, so CJK/Hangul/Cyrillic masters fit."""

from pathlib import Path

import pytest

from ns_common.embed_client import EMBED_WINDOW_TOKENS, FakeEmbed, approx_tokens, embed_window
from ns_common.errors import NsError
from ns_common.models import Story
from ns_studio.orchestrator import run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from tests.test_text_stages_master import _master_ctx

# Eight distinct sentences per language, rotated per chapter, plus one distinct ending per chapter.
BODY = {
    "ja": ["彼女は古い家の扉をゆっくりと開けました。", "廊下の奥から小さな物音が聞こえました。",
           "私は懐中電灯を握りしめて階段を上りました。", "壁にはたくさんの古い写真が掛かっていました。",
           "どの写真にも同じ男の人が写っていました。", "窓の外では冷たい雨が降り続けていました。",
           "机の上には黄色くなった手紙が置いてありました。", "その手紙には私の名前が書かれていました。"],
    "zh": ["她慢慢地推开了那扇旧房子的门。", "走廊尽头传来一阵轻微的响声。",
           "我紧紧握着手电筒走上了楼梯。", "墙上挂着许多泛黄的旧照片。",
           "每一张照片里都有同一个男人。", "窗外的冷雨一直下个不停。",
           "桌子上放着一封已经发黄的信。", "信封上写着我的名字。"],
    "ko": ["그녀는 낡은 집의 문을 천천히 열었습니다.", "복도 끝에서 작은 소리가 들렸습니다.",
           "나는 손전등을 꼭 쥐고 계단을 올랐습니다.", "벽에는 오래된 사진이 많이 걸려 있었습니다.",
           "모든 사진에는 같은 남자가 있었습니다.", "창밖에는 차가운 비가 계속 내리고 있었습니다.",
           "책상 위에는 누렇게 변한 편지가 놓여 있었습니다.", "그 편지에는 내 이름이 적혀 있었습니다."],
    "ru": ["Она медленно открыла дверь старого дома.", "В конце коридора послышался тихий шорох.",
           "Я крепко сжал фонарик и поднялся по лестнице.", "На стенах висело много старых фотографий.",
           "На каждой фотографии был один и тот же мужчина.", "За окном всё шёл холодный дождь.",
           "На столе лежало пожелтевшее письмо без марки.", "На конверте было написано моё имя."],
}
ENDINGS = {
    "ja": ["扉の向こうで誰かが笑いました。", "階段が突然きしみました。", "電気がすべて消えました。",
           "鏡の中の私は動きませんでした。", "そして朝が来ませんでした。"],
    "zh": ["门后有人轻轻笑了。", "楼梯突然响了一声。", "所有的灯都灭了。",
           "镜子里的我没有动。", "然后天再也没有亮。"],
    "ko": ["문 뒤에서 누군가 웃었습니다.", "계단이 갑자기 삐걱거렸습니다.", "불이 모두 꺼졌습니다.",
           "거울 속의 나는 움직이지 않았습니다.", "그리고 아침은 오지 않았습니다."],
    "ru": ["За дверью кто-то засмеялся.", "Лестница вдруг скрипнула.", "Весь свет разом погас.",
           "Моё отражение в зеркале не шевельнулось.", "И утро так и не наступило."],
}
SEP = {"ja": "", "zh": "", "ko": " ", "ru": " "}


def chapters_for(lang: str) -> list[dict]:
    body, sep = BODY[lang], SEP[lang]
    out = []
    for n in range(1, 6):
        picked = [body[(n + i) % len(body)] for i in range(len(body))]
        text = sep.join([*picked, ENDINGS[lang][n - 1]])
        out.append({"n": n, "title": f"{n}", "text": text, "summary": f"s{n}"})
    return out


def story_text(lang: str) -> str:
    return "\n\n".join(c["text"] for c in chapters_for(lang))


@pytest.mark.parametrize("lang", sorted(BODY))
def test_the_fixture_story_is_over_the_server_batch_unwindowed(lang: str):
    """Guards the fixture: embedding the whole story (or its first 1,500 characters) overflows 512 tokens."""
    text = story_text(lang)
    assert approx_tokens(text[:1500]) > 512


@pytest.mark.parametrize("lang", sorted(BODY))
def test_the_window_fits_the_server_for_every_script(lang: str):
    text = story_text(lang) * 3
    w = embed_window(text)
    assert text.startswith(w[:20]) and approx_tokens(w) <= EMBED_WINDOW_TOKENS < 512
    assert w.rstrip()[-1] in "。.！？!?"  # cut on a sentence boundary
    assert FakeEmbed().embed([w])  # the fake refuses an overlong input like llama-server


def test_the_window_counts_tokens_per_script():
    assert approx_tokens("a" * 400) == 100            # Latin: about four characters per token
    assert approx_tokens("字" * 400) == 400           # CJK: one token per character
    assert approx_tokens("한" * 400) == 400           # Hangul likewise
    assert approx_tokens("я" * 400) == 160            # Cyrillic: about 2.5 characters per token
    assert approx_tokens("") == 0


def test_the_window_is_capped_under_a_small_server_batch():
    text = "Дом стоял на холме. " * 400
    assert approx_tokens(embed_window(text, 256)) <= int(256 * 0.9)
    assert approx_tokens(embed_window(text, 2048)) <= EMBED_WINDOW_TOKENS


def test_a_single_overlong_sentence_is_cut_at_a_word_boundary():
    text = "слово " * 800  # no sentence end at all
    w = embed_window(text)
    assert approx_tokens(w) <= EMBED_WINDOW_TOKENS and w.endswith("слово")


def test_short_text_is_unchanged_and_latin_still_gets_a_long_window():
    assert embed_window("short") == "short"
    latin = "The old house was dark. " * 200
    assert approx_tokens(embed_window(latin)) > 400  # about 1,900 characters, not 1,500


def test_fake_embed_would_have_refused_the_unwindowed_story():
    for lang in BODY:
        with pytest.raises(NsError) as ei:
            FakeEmbed().embed([story_text(lang) * 3])
        assert ei.value.code == "NS-E316"


@pytest.mark.parametrize("lang", sorted(BODY))
def test_a_master_first_job_in_this_language_stores_its_embedding(
    lang: str, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real ContinuityStage and policy SelfSimilarity embed the opening window; before: NS-E316."""
    from ns_studio.policy.text_checks import SelfSimilarity

    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:4])
    ctx.channel = ctx.channel.model_copy(update={"language": lang})
    run_job(ctx)
    revised = ctx.read_json("story_revised.json")
    ctx.write_json("story_revised.json", {**revised, "chapters": chapters_for(lang)})
    ctx.services.embed = FakeEmbed()  # the stage must not exceed 512 tokens
    REGISTRY["continuity"].run(ctx)
    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.chapters[0].text == chapters_for(lang)[0]["text"]
    row = ctx.db.query("select embedding from stories where job_id=?", (ctx.job_id,))[0]
    assert row["embedding"]
    assert SelfSimilarity(skip_when_fake=False).run(ctx).passed is True
