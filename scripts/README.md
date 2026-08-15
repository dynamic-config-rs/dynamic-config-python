# Scripts

The `justfile` runs *checks*; these run *flows* — the git/gh choreography
around the checks. Each one is safe to re-run and says what it did.

| Script | What it does |
|---|---|
| `audit-report.py` | An OSV scan's findings, split into *a fix exists* (fails the job) and *no fix published* (a warning). Exemptions are `osv-scanner.toml`'s and never reach this. |
| `ci-local.sh` | The whole CI gate locally, in the order that fails fastest. |
| `claude-review-pr.sh` | Reviews a pull request with Claude locally — title, body, diff, and read-only access to the checkout. `--post` comments the review on the PR; without it, nothing leaves the terminal. |
| `dismiss-alert.sh` | Dismisses a Dependabot alert with the reason recorded *on the alert* — GitHub's UI leaves that nowhere a reviewer finds later. `--list` shows what is open. |
| `promote.sh` | `dev` → `main`: pushes, opens the pull request if it is not already open (titled "release X.Y.Z" when the push carries a version bump), arms auto-merge, waits for the gates, merges (squash), and re-syncs `dev` onto the new `main`. |
| `promotion-title.sh` | Sourced by the two scripts above — the one copy of the rule that titles a promotion. Not run by hand. |
| `propose.sh` | The first half of `promote.sh`: pushes `dev` and opens the pull request, then stops — for when something should read the PR before anything merges. |
| `release-python.sh` | Both wheels' release: bumps **both** versions, rotates both changelogs and moves the `dynamic-config-py>=…` floor between them, in one commit. `--check` refuses a version already on PyPI; `--status` says what is where. Prepares only — publishing stays CI's. |
| `resolve-python-audit.py` | What the wheels' extras actually resolve to today, as a requirements file. Neither package commits a lockfile — a library that pinned its dependencies would pin its users' — so the advisory scan and the SBOM resolve it here rather than reading a range and guessing. |
| `security-status.sh` | The whole security surface, read-only: open Dependabot alerts, open code-scanning findings, and cargo-deny's local view. Exits with the open-alert count. |
| `watch-ci.sh` | Watches the newest CI run for the current branch; on failure, prints the failed jobs' logs. |
| `watch-release.sh` | Watches the Release run the latest merge to `main` set off, and says how to recover from a rate limit. |

The release is a pull request too: `./scripts/release-python.sh patch`
prepares both wheels in one commit, `./scripts/promote.sh` lands it, and
the merge to `main` is what publishes. Nothing here talks to PyPI.
