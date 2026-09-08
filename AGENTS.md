# Instructions for Coding Agents

## Current phase

The documented Python utility and its standard-library test suite are implemented. Generated fixture data remains disposable and must not be committed.

Do not add dependencies, packaging files, or a PowerShell implementation unless the user explicitly requests that expansion.

When implementation is requested, treat these documents as the source of truth, in this order:

1. `docs/requirements.md` for normative product behavior.
2. `docs/technical-specification.md` for implementation decisions.
3. `docs/test-plan.md` for acceptance coverage and fixture lifecycle.
4. `docs/safety-and-recovery.md` for user-facing warnings and failure behavior.

If a requested change conflicts with the documents, update the affected documentation in the same change. Do not silently reinterpret a requirement.

## Non-negotiable safety rules

- Never describe rewritten commits as preserving their original SHA or signature.
- Never claim that all old commits are preserved. Older dates are bucketed unless they are already compressed (at most the configured density), in which case their existing commits may be preserved; after the seven-day boundary, the compressed source prefix is retained without recreation.
- Validate the full history reachable from `HEAD` for merge commits before presenting the rewrite as safe.
- Do not move, delete, rename, or force-update the source branch.
- Do not alter tags or remote-tracking refs.
- Do not use destructive worktree cleanup commands such as `git reset --hard` or `git clean`.
- `--force` may bypass the dirty-tree rejection and destination replacement prompt; it must not discard local changes.
- Create rewritten commits before publishing the destination ref, and update that ref with compare-and-swap protection.
- Refuse to update a destination branch checked out by another worktree.
- Treat repository paths and subprocess arguments as data. Invoke Git without a command shell.
- Do not add a dry-run option unless an audit-preview workflow is explicitly requested and documented.

## Python implementation rules

- Target a currently supported Python 3 release and use type annotations throughout.
- Dependencies are allowed when they materially improve correctness or maintainability. Declare every runtime dependency in `requirements.txt`; the current implementation uses `python-dotenv` for `.env` loading and the `git` executable from `PATH`.
- Prefer a small set of typed data structures and pure selection functions so bucket behavior can be unit tested independently.
- Preserve commit messages as bytes through the read/write path. Do not rebuild messages from the subject line.
- Pass author identity and author date explicitly to `git commit-tree`; do not preserve the original committer identity or timestamp.
- Sanitize inherited `GIT_AUTHOR_*` and `GIT_COMMITTER_*` variables before creating commits.
- Use `git -C <repository>` or an equivalent explicit subprocess working directory. Do not change the caller's global working directory.
- Return actionable errors without Python tracebacks for expected validation or Git failures.

## Test rules

- Use the standard-library `unittest` framework; do not introduce `pytest` or other third-party dependencies.
- Put tests and their dedicated disposable repository under `automated_test`.
- Never point test configuration at a repository outside the resolved `automated_test` directory.
- Generate commits with controlled author and committer dates, local user configuration, and deterministic file contents.
- Assert behavior through Git commands and repository state, not only through console text.
- Tests may delete and recreate only the resolved dedicated fixture directory after verifying it is beneath `automated_test`.
- Keep the source `master` ref unchanged and verify the rewritten tip has the same tree but a different object ID.

## Documentation and review checklist

Before considering an implementation complete:

- Ensure CLI help, environment keys, precedence, defaults, and examples agree across all documents.
- Run the full automated suite from the repository root.
- Manually inspect a generated branch with `git log --graph --format=fuller --all`.
- Confirm merge, dirty-tree, collision, invalid-config, and source-branch-preservation paths are covered.
- Report explicitly that recent SHAs change and old commit density is reduced.
