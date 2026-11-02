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
