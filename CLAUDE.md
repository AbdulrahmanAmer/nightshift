# Nightshift — working in this repo

- Spec: `BUILD_SPEC.md`. Plans: `docs/superpowers/plans/`. Read the plan index before starting work.
- **The laptop is for light local checks.** Use `D:\NightShift\venv\testvenv` for ruff, pyright and the touched
  test files (one pytest process at a time). Dev-mode and engine-test installs live under
  `D:\NightShift\installs`; scratch goes to `D:\NightShift\runs`, never C:. Heavy models, torch and GPU runtimes
  never go on the laptop; tests use fakes and tiny models.
- **CI is the verification of record** (`.github/workflows/ci.yml`). Never report a task as passing without the
  CI run URL and its conclusion.
- **Builders push and read CI.** A branch without a pull request gets no CI (push runs only for `main` and
  tags): push, open a pull request (a draft is fine), then `gh pr checks <n> --watch`; fix failures, commit,
  push again.
- **Never spawn Fable agents.** Spawned agents get an explicit cheaper model: `haiku` for sweeps and lookups,
  `sonnet` for ordinary code, `opus` only for hard design, verification or synthesis work.
- **Never download heavy models, torch, llama.cpp or whisper on a machine that is not the GPU target.** Tests use
  fakes (`ns_common` Fake* classes) and the FFmpeg that CI installs. Manifest values come from metadata APIs only.
- `contracts/` and `ns_common/` are edited only by the main session. Workers file `contracts/CHANGE_REQUESTS.md`.
- Stage files explicitly with `git add <paths>`; never `git add -A`.
- Every stage, client and CLI logs through `ns_common.logging.NsLogger`; errors are `NsError` with a code from
  `contracts/errors.json`.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), one task per commit.
- Gate scripts under `dev/` must keep working with `--fake-services` on a machine with no GPU.
