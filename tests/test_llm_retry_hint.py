"""F-H43 / D-37: the NS-E301 hint says only what the product does.

`ns_studio.text.llm_retry.transient` is the one rule for the story job's single retry. The errors.json hint
must describe exactly that rule, and must not promise a retry on the routes that have none (the Assistant's
`ns_dashboard/assistant/llm.py`, `ns verify`, `ns tune`'s benchmark).
"""

import httpx
import pytest

from ns_common.errors import NsError, describe
from ns_studio.text import llm_retry

DECIDED_HINT = (
    "Check that llama-server is healthy (ns doctor). A story job tries once more after a few seconds "
    "when the model sent a server error, the connection dropped or was refused, or the answer was empty; "
    "a timeout or a request the model rejects (a 4xx answer) is not tried again, "
    "so run the job again once the model answers."
)


def _status(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
    return httpx.HTTPStatusError(f"{code}", request=request, response=httpx.Response(code, request=request))


def _e301(cause: BaseException) -> NsError:
    try:
        raise NsError("NS-E301", "x") from cause
    except NsError as e:
        return e


def test_the_e301_hint_is_the_decided_text():
    assert describe("NS-E301").hint == DECIDED_HINT


@pytest.mark.parametrize(
    ("label", "cause", "retried"),
    [
        ("http 500", _status(500), True),
        ("http 503", _status(503), True),
        ("connect error", httpx.ConnectError("refused"), True),
        ("read error", httpx.ReadError("reset"), True),
        ("empty body key", KeyError("choices"), True),
        ("empty body value", ValueError("no json"), True),
        ("http 499", _status(499), False),
        ("http 400", _status(400), False),
        ("http 404", _status(404), False),
        ("read timeout", httpx.ReadTimeout("slow"), False),
    ],
)
def test_every_cause_the_hint_calls_retried_is_retried_and_no_other(label, cause, retried):
    assert llm_retry.transient(_e301(cause)) is retried, label


def test_the_hint_names_no_retry_for_the_routes_that_have_none():
    hint = describe("NS-E301").hint
    assert "will retry" not in hint.lower() or "story job" in hint
    assert "story job" in hint
    assert "timeout" in hint and "request the model rejects" in hint
