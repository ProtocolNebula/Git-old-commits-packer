# Normative Requirements

The terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are used normatively.

## 1. Purpose and scope

The product MUST be a standalone, typed Python command-line utility that creates a rewritten branch from the currently checked-out branch of a configured Git repository. Third-party dependencies MAY be used when they materially improve correctness or maintainability, and MUST be declared in the project dependency manifest.

The utility MUST reduce older history to a bounded number of representative commits per day and MUST replay recent history one commit at a time. It MUST NOT modify the source branch.

A dry-run/audit-preview mode is outside the initial scope.

## 2. Runtime and repository prerequisites

- Python 3 and Git MUST be available when the utility runs.
- Git MUST be invoked from `PATH`; the utility MUST NOT embed or download Git.
- The configured path MUST resolve to a Git worktree.
- `HEAD` MUST identify a checked-out named branch and at least one commit. Detached and unborn states MUST be rejected.
- Every commit reachable from `HEAD` MUST have at most one parent. The utility MUST reject the repository before rewriting if any merge commit is reachable.
- Tracked, staged, and untracked changes MUST cause rejection unless force mode is enabled.
- Force mode MUST NOT delete, reset, overwrite, or clean worktree changes.
- A usable Git committer identity MUST be configured before rewriting starts.

## 3. Configuration and command line

The utility MUST expose:

| CLI option | Environment key | Type | Requirement |
| --- | --- | --- | --- |
| `--repository-path PATH` | `REPOSITORY_PATH` | path | Required |
| `--unsquashed-days N` | `UNSQUASHED_DAYS` | integer | Required; `N >= 0` |
| `--max-commits-per-day X` | `MAX_COMMITS_PER_DAY` | integer | Required; `X > 0` |
| `--force` / `--no-force` | `FORCE` | boolean | Optional; defaults to false |
| `--env-file PATH` | n/a | path | Optional; defaults to `.env` beside the script |

- Explicit CLI options MUST override matching environment-file values.
- The implementation MUST use standard Python double-dash option spelling only. Both `--force` and `--no-force` MUST be accepted so a command-line value can override either `.env` boolean value.
- An explicitly supplied missing environment file MUST be an error. A missing default `.env` MAY be ignored if all required values are supplied on the command line.
- Relative `REPOSITORY_PATH` values read from a file MUST resolve relative to that file. A CLI repository path MUST resolve relative to the caller's working directory.
- Environment parsing MUST use the declared `python-dotenv` dependency and support its standard blank-line, comment, quoting, `export`, interpolation, and inline-comment behavior.
- Unknown keys MUST be ignored so ordinary environment files remain usable. A recognized key with no value and invalid values for recognized keys MUST be reported.
- `FORCE` MUST accept case-insensitive `true`, `false`, `1`, `0`, `yes`, and `no`.

## 4. Destination branch

- The destination MUST be named `squashed/YYYY_MM_DD`, using the machine-local calendar date captured once at startup.
- The utility MUST reject execution if the source branch already has that destination name.
- If the destination does not exist, the utility MUST create it only after all rewritten commits have been created successfully.
- If the destination exists and force mode is disabled, the utility MUST ask `Recreate <branch>? [y/N]`. Only a case-insensitive `y` or `yes` authorizes replacement. Empty input, EOF, and every other response MUST decline without moving the ref.
- Force mode MUST authorize destination replacement without prompting.
- Replacement MUST be guarded against concurrent ref changes using the previously observed destination object ID.
- The destination MUST NOT be updated if it is checked out in another linked worktree.
- After successfully publishing the ref, the utility MUST leave the source branch checked out. Checking out the destination is an explicit user or test-harness action.

## 5. Commit selection

- Commits MUST be processed in oldest-to-newest graph order along the linear history.
- The tip commit's author timestamp MUST anchor the cutoff.
- The cutoff instant MUST equal `tip_author_epoch - (UNSQUASHED_DAYS * 86,400 seconds)`.
- Every commit with an author timestamp greater than or equal to the cutoff MUST be selected individually.
- Each older commit MUST be assigned to an author-local calendar date and one of `MAX_COMMITS_PER_DAY` equal-width slots within that date.
- The selected representative for an occupied old-history slot MUST be the last commit in graph order assigned to that slot.
- Selected representatives MUST be recreated in original graph order. This MUST yield no more than `MAX_COMMITS_PER_DAY` selected old commits for any author-local date.
- The tip MUST always be selected, including when `UNSQUASHED_DAYS` is zero.

## 6. Recreated commits

Each selected source commit MUST provide:

- its exact tree object;
- its complete commit message;
- author name and email;
- author timestamp and recorded UTC offset.

Each recreated commit MUST use the previously recreated commit as its sole parent. The oldest selected commit MUST have no parent.

Original committer identity and committer timestamp MUST NOT be preserved. Git MUST generate new committer metadata from current repository/user configuration and the rewrite time.

Cryptographic signatures and other unlisted commit headers are not preserved. Every recreated commit receives a new object ID. The rewritten tip's tree MUST equal the source tip's tree.

## 7. Ref and worktree guarantees

- The source local branch ref MUST remain at its original object ID.
- Local branches other than the destination, tags, remote-tracking refs, remotes, and repository configuration MUST remain unchanged.
- The utility MUST NOT push rewritten history.
- Commit construction MAY leave unreachable objects if a later operation fails, but visible refs MUST remain unchanged until construction completes.
- Destination publication has no checkout phase; a successful publication leaves the source branch checked out.

## 8. User-visible results

On success, output MUST include:

- source and destination branch names;
- source and recreated commit counts;
- cutoff instant;
- confirmation that the source ref was retained;
- a warning that all recreated SHAs differ and older commits were sampled.

Expected configuration, validation, and Git errors MUST be concise and actionable and MUST NOT show an unhandled Python traceback.

## 9. Mandatory automated test

- Automated coverage MUST live under `automated_test` and use `.env.testing` settings.
- The dedicated fixture MUST use branch `master` and 30 commits distributed at eight-hour intervals over ten calendar days.
- If the fixture directory or its `master` branch does not exist, the harness MUST safely recreate the dedicated repository.
- Otherwise, it MUST record the entry branch, check out `master`, and delete the entry branch only when it is not `master`.
- The test configuration MUST permit non-interactive replacement of a previous dated destination.
- The suite MUST establish the behavioral and safety assertions in `docs/test-plan.md`.
