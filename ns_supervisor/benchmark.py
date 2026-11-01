"""C2 LLM speed probe shared by `ns verify` and `ns tune`: llama-server `/completion` timings."""

from __future__ import annotations

import httpx

from ns_common.errors import NsError

# ~400 tokens of fixed text, so every measurement sees the same prefill (spec 3: 400-token prompt).
_SENTENCE = "The lighthouse keeper counted the waves again and wrote the number in his book. "
BENCH_PROMPT = _SENTENCE * 24 + "Continue the story:"
BENCH_N_PREDICT = 300


def measure_tok_s(
    base_url: str,
    *,
    n_predict: int = BENCH_N_PREDICT,
    prompt: str = BENCH_PROMPT,
    timeout_s: float = 600,
    client: httpx.Client | None = None,
) -> float:
    """Generation speed (tokens/s) reported by `POST /completion` (`timings.predicted_per_second`).

    Raises `NS-E301` when the server does not answer or reports no timings.
    """
    http = client or httpx.Client(timeout=timeout_s)
    body = {
        "prompt": prompt, "n_predict": n_predict, "temperature": 0, "cache_prompt": False, "stream": False,
    }  # fmt: skip
    try:
        r = http.post(f"{base_url.rstrip('/')}/completion", json=body)
        r.raise_for_status()
        tok_s = float(r.json()["timings"]["predicted_per_second"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as e:
        raise NsError("NS-E301", f"/completion benchmark failed: {e}") from e
    return tok_s
