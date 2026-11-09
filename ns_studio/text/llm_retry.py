"""One studio-side retry of an NS-E301 (the errors.json hint promises "The job will retry once").

`ns_common.llm_client.LlmClient` raises NS-E301 on any transport error, any HTTP error status and an answer
with no content. A busy or just restarted llama-server answers a 5xx or an empty body now and then; one more
try after a short wait usually works. A timeout (600 s) or a 4xx is not retried: the first would hold the GPU
for twenty more minutes, the second would fail the same way.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from ns_common.errors import NsError
from ns_studio.context import JobContext

RETRY_WAIT_S = 3.0
_sleep = time.sleep


def transient(err: NsError) -> bool:
    """True for an NS-E301 worth one more try: a 5xx, a refused or reset connection, an unusable body."""
    if err.code != "NS-E301":
        return False
    cause = err.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return False
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code >= 500
    return isinstance(cause, httpx.TransportError | KeyError | IndexError | ValueError)


def retry_once[T](ctx: JobContext, call: Callable[[], T]) -> T:
    """`call()`; after a transient NS-E301 wait `RETRY_WAIT_S` and call once more (a second one is raised)."""
    try:
        return call()
    except NsError as e:
        if not transient(e):
            raise
        ctx.log.warn("llm_retry", code=e.code, detail=e.detail[:200], wait_s=RETRY_WAIT_S, job_id=ctx.job_id)
        _sleep(RETRY_WAIT_S)
        return call()
