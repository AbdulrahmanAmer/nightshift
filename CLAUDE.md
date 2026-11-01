# Nightshift — working in this repo

- Spec: `BUILD_SPEC.md`. Plans: `docs/superpowers/plans/`. Read the plan index before starting work.
- **Nothing runs on the authoring laptop.** Do not create a venv, and do not run pip, python, pytest, ruff or
  pyright locally. Write code and tests, commit, push.
- **Tests run in GitHub Actions** (`.github/workflows/ci.yml`: ruff, pyright, pytest on windows-latest on every
  push). Verification is the CI result; never report a task as passing without the CI run URL and its conclusion.
- **Builders push and read CI.** After each task or two: `git push`, then `gh run watch --exit-status`
  (or `gh run list --limit 1` and `gh run view <id> --log-failed`), fix failures, commit, push again.
- **Never spawn Fable agents.** Spawned agents get an explicit cheaper model: `haiku` for sweeps and lookups,
  `sonnet` for ordinary code, `opus` only for hard design, verification or synthesis work.
- **Never download models, torch, llama.cpp or whisper on a machine that is not the GPU target.** Tests use fakes
  (`ns_common` Fake* classes) and the FFmpeg that CI installs. Manifest values come from metadata APIs only.
- `contracts/` and `ns_common/` are edited only by the main session. Workers file `contracts/CHANGE_REQUESTS.md`.
- Stage files explicitly with `git add <paths>`; never `git add -A`.
- Every stage, client and CLI logs through `ns_common.logging.NsLogger`; errors are `NsError` with a code from
  `contracts/errors.json`.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), one task per commit.
- Gate scripts under `dev/` must keep working with `--fake-services` on a machine with no GPU.
