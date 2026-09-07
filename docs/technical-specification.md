# Technical Specification

## 1. Planned layout

The initial implementation should remain small and dependency-free:

```text
squash_old_commits.py
automated_test/
  .env.testing
  test_squash_old_commits.py
  repository/                 # generated, disposable nested Git fixture
```

Production behavior belongs in the single top-level script. Selection and parsing logic should be importable without invoking the CLI. The generated fixture must not be committed.

## 2. Typed model

Use typed records equivalent to:

- `Config`: repository root, retention days, maximum daily density, force flag, and selected env-file path.
- `SourceCommit`: object ID, tree ID, full message bytes, author name/email, author epoch, and author UTC offset.
- `BucketKey`: author-local ISO date and integer slot.
- `RewriteResult`: source/destination refs, counts, cutoff, rewritten tip, and checkout status.

Keep configuration loading, commit selection, and bucket calculation free of Git mutations so they can be unit tested directly.

## 3. Configuration flow

1. Parse CLI arguments while retaining whether each value was explicitly supplied.
2. Resolve `--env-file`; otherwise use `.env` beside the script.
3. Parse recognized keys from the file when present.
4. Overlay explicit CLI values.
5. Apply `FORCE=false` when absent.
6. Validate types and ranges, then normalize the repository path with `resolve()`.

Quotes delimit the entire environment value and are removed. Inline comments, variable expansion, `export KEY=...`, multiline values, and escape interpolation are not supported. This limitation must appear in CLI help.

## 4. Preflight sequence

All checks occur before commit construction:

1. Verify Git is executable and the configured directory belongs to a worktree with `git -C <path> rev-parse --show-toplevel`.
2. Normalize operations to the returned worktree root.
3. Resolve the source branch with `symbolic-ref --quiet --short HEAD` and verify `HEAD` exists.
4. Capture the source object ID immediately and use that immutable ID for all subsequent reads.
5. Check cleanliness with porcelain v1, NUL-delimited output, including untracked files. Skip only this rejection in force mode.
6. Scan `rev-list --parents <source_oid>` and reject any row containing more than a commit and one parent.
7. Validate committer configuration with `git var GIT_COMMITTER_IDENT` after removing inherited Git identity variables.
8. Capture the machine-local date and derive the destination name.
9. Inspect linked worktrees and reject if the destination is checked out anywhere else.
10. Resolve the destination ref, then prompt or apply force-mode collision policy before creating objects.

Run subprocesses using argument arrays, captured output, and `shell=False`. Set `GIT_TERMINAL_PROMPT=0` for commands that must not trigger unrelated Git credential prompts.

## 5. Reading history

Read commits reachable from the captured source object ID in reverse order so the in-memory sequence is oldest first. Do not resolve `HEAD` repeatedly after preflight.

For every commit, retain the original tree, complete message bytes, author identity, author epoch, and numeric offset. Avoid subject-only formats or line-oriented parsing for messages. Parsing raw commit objects or an equivalently lossless NUL-safe Git interface is acceptable.

The implementation may assume author names and emails are valid text accepted by the host process environment. Commit messages must remain bytes and be passed unchanged to `git commit-tree`.

## 6. Cutoff and bucketing algorithm

Let `tip_epoch` be the author epoch of the final source commit:

```text
cutoff_epoch = tip_epoch - unsquashed_days * 86400
```

For an old commit, convert its author epoch using that commit's recorded fixed UTC offset. Let `seconds` be the resulting wall-clock seconds since local midnight, in the inclusive range `0..86399`. Calculate:

```text
slot = floor(seconds * max_commits_per_day / 86400)
bucket = (local_date, slot)
```

This supports any positive density, not only divisors of 24. Clamp the calculated slot to `max_commits_per_day - 1` defensively.

Walk commits oldest first:

- add every index with `author_epoch >= cutoff_epoch` to the selected-index set;
- for every older commit, assign `last_old_index[bucket] = index`, replacing an earlier representative;
- add all final bucket representative indices to the set;
- sort selected indices numerically before recreation.

Selection is based on timestamps, but ordering is always based on the commit graph. Non-monotonic author dates therefore do not reorder commits or exceed one representative per occupied bucket.

## 7. Commit construction

Create selected commits with `git commit-tree` from oldest to newest:

1. Use the selected source tree unchanged.
2. Supply `-p <rewritten_parent>` except for the first selected commit.
3. Send the complete source message through standard input as bytes.
4. Set `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, and `GIT_AUTHOR_DATE` from the source author. Use a date form containing both epoch and original numeric offset.
5. Remove all inherited `GIT_AUTHOR_*` and `GIT_COMMITTER_*` variables before adding the intended author values.
6. Leave committer values unset so Git uses configured identity and the current time.

Do not copy signatures, merge parents, encoding headers, mergetags, or source committer fields. Record every returned object ID and abort on the first failed Git command.

## 8. Publishing and checkout

No visible ref changes occur while commits are being built.

- For a new destination, use `update-ref` with the all-zero expected old object ID.
- For a replacement, use the captured destination object ID as the expected old value.
- Include a descriptive reflog message.
- A compare-and-swap failure is a concurrency error; do not retry against an unreviewed new value.
- After publication, run `git switch <destination>` without reset, clean, or discard flags.

Because the selected tip uses the original tip tree, ordinary dirty changes allowed by force mode can carry across the branch switch without being discarded. If Git still refuses the switch, retain both refs and report that the destination was created but not checked out.

## 9. Failure and output conventions

Use standard error for diagnostics and standard output for the final summary. Expected failures should have a nonzero exit status and one leading `error:` message followed, when useful, by a safe remediation.

Differentiate these outcomes in text:

- no ref changed because validation, confirmation, construction, or compare-and-swap failed;
- destination ref changed but checkout failed;
- complete success with destination checked out.

Never imply preservation of source object IDs. On success, state that the original source branch is still available for comparison or recovery.

