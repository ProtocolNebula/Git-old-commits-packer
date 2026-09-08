# Git Backup Packer

This repository specifies a standalone Python utility that rewrites a checked-out, linear Git history onto a new branch named `squashed/YYYY_MM_DD`.

The utility will keep the most recent configured number of days as individual commits. Older history will be represented by at most a configured number of commits per author-local calendar day. The source branch is never moved or deleted.

> [!WARNING]
> This is a history rewrite. Every recreated commit receives a new object ID, including recent commits that are replayed individually. Older dates are bucketed unless they are already compressed at the configured density; commits omitted by that selection are not preserved on the rewritten branch.

## Repository status

The production script and standard-library integration tests are implemented. The generated Git fixture under `automated_test/repository` is disposable and ignored by Git.

## Command-line interface

```text
python squash_old_commits.py \
  [--env-file PATH] \
  [--repository-path PATH] \
  [--unsquashed-days N] \
  [--max-commits-per-day X] \
  [--force | --no-force] \
  [--force-recheck-all | --no-force-recheck-all]
```

Configuration keys:

```dotenv
REPOSITORY_PATH=/path/to/repository
UNSQUASHED_DAYS=14
MAX_COMMITS_PER_DAY=2
FORCE=false
FORCE_RECHECK_ALL=false
```

Explicit command-line values take precedence over values in the selected environment file. Install runtime dependencies with `python -m pip install -r requirements.txt`.

Copy [.env.example](.env.example) to `.env`, or pass `--env-file PATH`. Run the tests with:

## Automated testing

```text
python -m unittest discover -s automated_test -p "test_*.py" -v
```

For the bundled ten-day fixture, use the testing configuration to see a reduction:

```text
git -C automated_test/repository switch master
python squash_old_commits.py --env-file automated_test/.env.testing
git -C automated_test/repository rev-list --count master
git -C automated_test/repository branch --list "squashed/*"
```

`master` remains at 30 commits and stays checked out. The destination count depends on the `UNSQUASHED_DAYS` and `MAX_COMMITS_PER_DAY` values in `.env.testing` (for example, 23 with 3/2, or 13 with 1/1). If `UNSQUASHED_DAYS=14` is used with this ten-day fixture, all commits are within the retention window and no reduction is expected.

When more than seven consecutive older dates already contain at most `MAX_COMMITS_PER_DAY` commits, the utility applies its finish rule while scanning newest-to-oldest: it stops recompressing at that existing prefix, retains it unchanged, and processes only newer commits. If there are no newer commits, it exits successfully without creating a destination ref. Pass `--force-recheck-all` (or set `FORCE_RECHECK_ALL=true`) to recheck the full history.

## Git flow

The utility does not use `git rebase`, `git merge`, or `git cherry-pick`. It reads the source commits, creates replacement commits with `git commit-tree`, and publishes the result as a new branch.

Reference invocation:

```text
python squash_old_commits.py --env-file automated_test/.env.testing --force
```

The flow is:

```text
checked-out source branch
        |
        v
validate repository and linear history
        |
        v
read commits and select representatives
        |
        v
create replacement commits with commit-tree
        |
        v
verify the source branch did not move
        |
        v
publish squashed/YYYY_MM_DD
```

Git is invoked with `-c safe.directory=<repository>` and an explicit repository working directory. The caller's working directory is not changed.

### Validation and reading

The main validation commands are:

```text
git rev-parse --show-toplevel
git symbolic-ref --quiet --short HEAD
git rev-parse --verify HEAD^{commit}
git status --porcelain=v1 -z --untracked-files=all
git var GIT_COMMITTER_IDENT
```

The source history is read with:

```text
git rev-list --reverse --parents <source-tip>
git cat-file --batch
```

The ordered commit IDs from `rev-list` are sent to one persistent batch request. This avoids starting one `git cat-file` process per source commit. The length-delimited responses preserve complete commit-message bytes safely, including embedded newlines.

The script checks for merge commits and retains each selected commit's tree, complete message, author identity, author timestamp, and author offset.

### Selection and recreation

Selection is performed in Python. Recent commits are replayed individually. Older commits are grouped by author-local date and time buckets; the last commit in each occupied bucket is selected.

The scan runs newest-to-oldest. When it finds more than seven adjacent dates already at or below `MAX_COMMITS_PER_DAY`, that older prefix is retained without recreation. Only commits newer than the boundary are recreated. `--force-recheck-all` disables this prefix-reuse rule.

Each recreated commit is made with:

```text
git commit-tree <tree-id> [-p <parent-id>]
```

The complete source message is sent through standard input. Author values are supplied through `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, and `GIT_AUTHOR_DATE`; Git generates new committer metadata. Therefore recreated commits have new SHAs.

### Publication

Before publishing, the script verifies the source ref again:

```text
git rev-parse refs/heads/<source-branch>
```

The destination is updated atomically with compare-and-swap:

```text
git rev-parse --show-object-format
git update-ref refs/heads/squashed/YYYY_MM_DD <new-tip> <expected-old-tip> -m "squash old commits"
```

For a new destination, `<expected-old-tip>` is all zeroes. For replacement, it is the destination SHA observed before construction. The source branch remains checked out and unchanged; the script does not switch to the destination.

## Preconditions

The target must:

- be a Git worktree with a checked-out, non-unborn branch;
- have entirely linear history reachable from `HEAD`;
- have no tracked or untracked changes unless `--force` is supplied;
- have Git available on `PATH` and a usable committer identity.

Always preserve or back up important refs before testing a history-rewriting tool. Review merge history before treating this utility as safe for a repository.

## Documentation

- [Requirements](docs/requirements.md) defines the normative behavior and acceptance criteria.
- [Technical specification](docs/technical-specification.md) defines the interfaces, selection algorithm, data flow, and failure handling.
- [Test plan](docs/test-plan.md) defines the mandatory reusable integration fixture and test scenarios.
- [Safety and recovery](docs/safety-and-recovery.md) explains risks, guarantees, and recovery expectations.
- [AGENTS.md](AGENTS.md) contains constraints for future coding agents working in this repository.


# About AI usage

Script has been vibecoded and just partially revised the code. Run under your responsability. Tested manually (by human) and with automated testing (generated by AI)
