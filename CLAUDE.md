# Nightshift — working in this repo

- Spec: `BUILD_SPEC.md`. Plans: `docs/superpowers/plans/`. Read the plan index before starting work.
- **The laptop is for light local checks.** Use `D:\NightShift\venv\testvenv` for ruff, pyright and the touched
  test files (one pytest process at a time). Dev-mode and engine-test installs live under
  `D:\NightShift\installs`; scratch goes to `D:\NightShift\runs`, never C:. Heavy models, torch and GPU runtimes
  never go on the laptop; tests use fakes and tiny models.
- **Work moves in blocks** per `D:\NightShift\refs\OPERATING.md` (on this machine; the file is outside the
  repository). A builder builds, tests and commits locally in its worktree and never pushes or opens a pull
  request. The Head integrates a block, and CI on the block's one pull request is the verification of record: never
  report a block as passing without that run's URL and its conclusion.
- **Never spawn Fable agents.** Spawned agents get an explicit cheaper model: `haiku` for sweeps and lookups,
  `sonnet` for ordinary code, `opus` only for hard design, verification or synthesis work.
- **Never download heavy models, torch, llama.cpp or whisper on a machine that is not the GPU target.** Tests use
  fakes (`ns_common` Fake* classes) and the FFmpeg that CI installs. Manifest values come from metadata APIs only.
- Shared code (`ns_common/`, `contracts/`, `pyproject.toml`) changes only in an item whose card says "shared".
- Stage files explicitly with `git add <paths>`; never `git add -A`.
- Every stage, client and CLI logs through `ns_common.logging.NsLogger`; errors are `NsError` with a code from
  `contracts/errors.json`.
- Conventional commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`), one task per commit.
- Gate scripts under `dev/` must keep working with `--fake-services` on a machine with no GPU.
