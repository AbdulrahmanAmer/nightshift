import httpx

from ns_common.embed_client import EmbedClient, FakeEmbed, cosine, from_blob, to_blob


def test_fake_embed_is_deterministic_and_meaningful():
    e = FakeEmbed()
    a = e.embed(["the old house was dark"])[0]
    b = e.embed(["the old house was dark"])[0]
    c = e.embed(["a sunny beach party"])[0]
    assert a == b
    assert cosine(a, b) > 0.999
    assert cosine(a, c) < 0.3


def test_blob_roundtrip():
    v = [0.1, -0.5, 2.0]
    assert [round(x, 5) for x in from_blob(to_blob(v))] == v


def test_client_parses_openai_shape():
    def handler(req: httpx.Request) -> httpx.Response:
        data = [{"index": 0, "embedding": [1.0, 0.0]}, {"index": 1, "embedding": [0.0, 1.0]}]
        return httpx.Response(200, json={"data": data})

    cli = EmbedClient("http://127.0.0.1:8081", client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = cli.embed(["a", "b"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]
    assert round(cosine(out[0], out[1]), 6) == 0.0


def test_fake_embed_rejects_inputs_llama_server_would_reject():
    import pytest

    from ns_common.embed_client import MAX_EMBED_TOKENS, approx_tokens
    from ns_common.errors import NsError

    e = FakeEmbed()
    long_text = "word " * 2000  # a master story is about 9,000 characters
    assert approx_tokens(long_text) > MAX_EMBED_TOKENS
    with pytest.raises(NsError) as ei:
        e.embed([long_text])
    assert ei.value.code == "NS-E316"
    assert e.embed(["fine " * 100])  # 500 characters pass


def test_embed_window_cuts_at_a_word_boundary():
    from ns_common.embed_client import EMBED_CHARS, approx_tokens, embed_window

    text = "alpha beta gamma " * 400
    w = embed_window(text)
    assert len(w) <= EMBED_CHARS and not w.endswith("alph") and text.startswith(w)
    assert approx_tokens(w) <= 512
    assert embed_window("short") == "short"


def test_embed_server_args_cover_the_window():
    from ns_common.config import embed_server_args
    from ns_common.models import Runtime

    args = embed_server_args(Runtime.default("cpu-dev").embed)
    assert args == ["-c", "2048", "-b", "2048", "-ub", "2048"]


def test_embed_failures_have_their_own_code_not_the_llm_one():
    import pytest

    from ns_common.errors import NsError

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def junk(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    for handler in (down, junk):
        transport = httpx.MockTransport(handler)
        cli = EmbedClient("http://127.0.0.1:8081", client=httpx.Client(transport=transport))
        with pytest.raises(NsError) as ei:
            cli.embed(["a"])
        assert ei.value.code == "NS-E316"
