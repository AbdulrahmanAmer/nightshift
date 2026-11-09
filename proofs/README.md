# proofs/: box proofs as code (order H-13)

A proof script shows one box (or a small group) of `docs/FINALIZATION.md` / `docs/FEATURE_STATUS.md` working on an
install with fake services. The runner `dev/prove.py` (Builders) finds every script here and runs it against a
home:

    python dev/prove.py --home D:\NightShift\installs\dev --list
    python dev/prove.py --home D:\NightShift\installs\dev --box F-B7
    python dev/prove.py --home D:\NightShift\installs\dev --all

It writes one evidence file per box, `<runs>/proofs/<version>/<box>.json`. A tick cites the proof id and that
evidence. The scripts are owned by QA and the runner by Builders.

## A proof script

`proofs/<slug>.py`: lower-case box ids plus a word, e.g. `f_b7_review_reject.py`. Files and folders that start
with `_` are not proofs (`_lib/` holds shared helpers).

```python
PROOF = {
    "id": "f-b7-review-reject",  # unique, cited by the tick
    "boxes": ["F-B7"],  # the box ids this proof shows
    "install": "dev",  # "dev" | "engine" | "any" (runtime.json of the home)
    "window": False,  # True: drives a page (Playwright, Edge) or a native window (UI Automation)
    "needs": ["engine"],  # services expected up: "engine" = supervisor + dashboard; [] = none
    "timeout_s": 300,
    "fakes": [],  # fake endpoints the proof starts itself: "footage", "github"
}


def run(ctx) -> None: ...
```

A proof passes by returning. It fails by raising (`ctx.check` raises `ProofFailed`). `ctx.skip(reason)` ends it as
skipped, which is never a pass.

### What `ctx` gives

| Member | What it is |
|---|---|
| `ctx.home`, `ctx.version`, `ctx.kind` | the install's home `Path`, its `VERSION`, `"dev"` or `"engine"` |
| `ctx.ports` | `{"dashboard": 8100, "supervisor": 8099, ...}` from the install's config |
| `ctx.scratch` | an empty folder `<runs>/proofs/<version>/<proof id>/` (never on C:) |
| `ctx.api(method, path, body=None, timeout=120)` | dashboard HTTP with no proxy: `(status, json or text)` |
| `ctx.cli(args, timeout)` | `ns` / `studio` from the install's own venv, `NIGHTSHIFT_HOME` set to `ctx.home` |
| `ctx.spawn(args, env=None, name=None)` | a helper process (fake proxy, watcher); its pid is recorded and the runner stops exactly that pid at the end |
| `ctx.db(sql, params=())` | read-only rows from `<home>/data/studio.db` |
| `ctx.wait(fn, timeout, every=2, what="")` | polls until `fn()` is truthy; raises `ProofFailed` naming `what` on timeout |
| `ctx.step(name, **observed)` | one evidence step with its observed values |
| `ctx.check(cond, what, **observed)` | an evidence step with pass or fail; raises `ProofFailed` when false |
| `ctx.page()` | window proofs only: a Playwright page on Edge at the dashboard; the runner closes it |
| `ctx.skip(reason)` | ends the proof as skipped |

### Rules a proof keeps

- It ends at the effect: a file, a database row, an HTTP answer the page reads, or text read off the page through
  Playwright or UI Automation. A log line that says something happened is not the effect.
- It starts and stops only what it started (`ctx.spawn`); no kill by name or command line.
- No global input of any kind (no SendKeys, pyautogui, keybd_event, clipboard paste). Pages go through Playwright
  on the page itself; a native dialog goes through UI Automation bound to that dialog's own handle.
- It never presses a button that writes to disk on a timeout or fallback path.
- It changes no state to make itself pass. It creates what it needs (its own channel, its own job) under a name
  starting with `proof-`, and says so in a step.
- Nothing is written to C:. Downloads, renders and scratch go to `ctx.scratch` or the install's home.
- No secret is written to the evidence. Fake keys are fake (`GOODKEY`-style), and a real token never appears.
- No private key or certificate is committed: the fake endpoints make theirs in `ctx.scratch` at run time.
