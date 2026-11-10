# proofs/: box proofs as code (order H-13)

A proof script shows one box (or a small group) of `docs/FINALIZATION.md` / `docs/FEATURE_STATUS.md` working on an
install with fake services. The runner `dev/prove.py` (Builders) finds every script here and runs it against a
home:

    python dev/prove.py --home D:\NightShift\installs\dev --list
    python dev/prove.py --home D:\NightShift\installs\dev --box F-B7
    python dev/prove.py --home D:\NightShift\installs\dev --all
    python dev/prove.py --home D:\NightShift\installs\dev --all --strict    # the release rule

`--strict`: a skipped proof is not a passed one. The run exits 3 when none failed but any selected proof
skipped, and the summary names each skipped proof and why. Without it a skip leaves the exit code at 0.

### What install the release rule runs on

`--all --strict` is run on a dev install with fake services and its engine up, where every proof can run:

- FFmpeg is findable the way the product finds it (`NS_FFMPEG`, PATH, `<home>/bin/ffmpeg`), and `openssl` (beside
  Git for Windows) or the `cryptography` package is importable: without them F-B13/F-B14 skip.
- **No real stock-footage key (Pexels or Pixabay) is stored in that install.** F-B13/F-B14 skip by design over a
  real key ("the proof never replaces a real key"), and a skip is exit 3 under `--strict`. An install where
  keys were stored for other checks therefore fails the rule every time: clear the keys in Settings first, or
  run the rule on a fresh dev install (a "proof home") that never had one.
- The Python is one with the repository's dependencies (the repository test venv).

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

## Every proof has a test (D-43)

A proof script that has never run reaches a release candidate with its own bugs (F-B7 looked for the job id in
a path that carries the title; F-B13/F-B14 indexed a tuple like a dict). So the rule is:

> Every proof script has a test in `tests/test_proof_scripts.py` that runs it to its end against `proof_home`,
> and a proof without one fails CI (`test_every_proof_script_has_a_fake_home_test`).

`proof_home` (`tests/proofhome.py`) is a fake-services home made by the product's own code, with the real
dashboard app over it on its own port; the proof runs through the real runner (`dev.prove.Runner`). To add
a proof:

1. Write `proofs/<slug>.py` as above.
2. Add `def test_<slug>_passes_on_the_fake_services_home(proof_home)` to `tests/test_proof_scripts.py`:
   `result = proof_home.run("<slug>")`, then `assert_passed(result)`. The test runs the proof's own code
   (`proof_home.run` does); never replace what the proof checks with a stand-in.
3. Add its row to `COVERED` there: `{"passes": <test name>, "fails": [<test names>]}`. The `fails` tests put one
   fault the proof exists to catch into the home (patch the product, as the existing ones do) and call
   `assert_fails_at(result, "<text of the check that must fail>")`; a proof without one fails CI. If a proof
   cannot be made to fail for its fault, strengthen the proof (F-C1 once compared only the version).
4. A proof that starts a helper needs `proof_home.allow_spawn = True` in its test; `HomeCtx.spawn` in
   `tests/proofhome.py` says which helpers it answers. A proof that needs FFmpeg runs its real FFmpeg in CI
   (CI installs it).

A proof that looks for FFmpeg uses `proofs._lib.ffmpeg_path(ctx.home)` (the product's own lookup), never
`shutil.which`.
