# Repository settings

Settings that live in GitHub rather than in files. Owner: C10. Rationale: `docs/ENGINEERING.md` section 7.
Applied on 2026-11-01 with `gh api` using the owner's token; every command is repeatable.

## Branch protection on `main` (applied)

| Setting | Value | Why |
|---|---|---|
| Required status checks | `lint`, `types`, `test` | The three jobs of `.github/workflows/ci.yml` that decide merge safety. `powershell-parse`, `bundle` and `scope-check` report but do not block yet. |
| Require branches to be up to date | off | Builders on disjoint paths would otherwise rebase constantly. |
| Force pushes | forbidden | History of published branches is never rewritten (ENGINEERING section 5). |
| Branch deletion | forbidden | |
| Pull request reviews | not required | A solo orchestrator merges; the checks are the gate. |
| Include administrators | off | The owner token pushes to `main` directly when file sets are disjoint (ENGINEERING section 5); with this off, admins bypass the required checks. Turn it on once every merge goes through a pull request. |

```powershell
@'
{
  "required_status_checks": {"strict": false, "contexts": ["lint", "types", "test"]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
'@ | gh api -X PUT repos/AbdulrahmanAmer/nightshift/branches/main/protection --input -
```

Result of the call: HTTP 200; `required_status_checks.contexts = ["lint","types","test"]`, `allow_force_pushes = false`,
`allow_deletions = false`, `enforce_admins = false`.

## Tag ruleset (applied)

Ruleset `protect-release-tags` (id 26322610), target `tag`, refs `refs/tags/v*` and `refs/tags/contracts-m*`,
rules `update`, `deletion`, `non_fast_forward`; the repository admin role bypasses. Effect: a published release tag or
contract freeze tag cannot be moved or deleted; only the owner can create one.

```powershell
@'
{
  "name": "protect-release-tags", "target": "tag", "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*", "refs/tags/contracts-m*"], "exclude": []}},
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
  "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}]
}
'@ | gh api -X POST repos/AbdulrahmanAmer/nightshift/rulesets --input -
```

## Actions permissions (applied)

Default `GITHUB_TOKEN` permissions are read-only (`default_workflow_permissions=read`); workflows that publish declare
`contents: write` on the single job that needs it (`release.yml` job `release`, `build-qwentts.yml` job `release-asset`).
No secrets are stored in the repository.

```powershell
gh api -X PUT repos/AbdulrahmanAmer/nightshift/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

## Workflows

| File | Trigger | What |
|---|---|---|
| `.github/workflows/ci.yml` | push, pull request | `lint`, `types`, `test`, `powershell-parse`, `bundle`; `scope-check` on pull requests only |
| `.github/workflows/release.yml` | tag `v*` | tests, `dev/make_bundle.ps1`, GitHub Release with `Nightshift-<ver>.zip`, `.sha256`, `nightshift-app-<ver>.zip` |
| `.github/workflows/build-qwentts.yml` | `workflow_dispatch`, tag `v*` | builds `qwentts-win-cuda-x64.zip` (see below) |

Cutting a release (orchestrator): move the `## [Unreleased]` entries in `CHANGELOG.md` under `## [x.y.z]`, set `VERSION`,
`pyproject.toml` and `ns_common.__version__` to `x.y.z`, merge to `main`, wait for green CI, then
`git tag vX.Y.Z && git push origin vX.Y.Z`. `release.yml` fails when the tag, `VERSION` or the changelog heading disagree.

## qwentts build

QWENTTS_BUILD_STATUS_PLACEHOLDER
