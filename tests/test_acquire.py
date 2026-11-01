import pytest

from ns_common.acquire import DevAcquirer
from ns_common.errors import NsError
from ns_common.models import Runtime


def test_dev_acquirer_endpoints_and_lock():
    acq = DevAcquirer(Runtime.default("cpu-dev"))
    ep = acq.acquire("llama", "studio:j1", model_alias="story")
    assert ep.base_url == "http://127.0.0.1:8080" and ep.model_alias == "story"
    assert acq.acquire("embed", "studio:j1").base_url == "http://127.0.0.1:8081"   # embed never locks
    with pytest.raises(NsError) as ei:
        acq.acquire("tts", "studio:j2")      # GPU already held by j1 via llama
    assert ei.value.code == "NS-E201"
    acq.release("llama", "studio:j1")
    assert acq.acquire("tts", "studio:j2").base_url == "http://127.0.0.1:8091"
    acq.release("tts", "studio:j2")
    assert acq.acquire("whisper", "studio:j2").base_url == "inproc"
