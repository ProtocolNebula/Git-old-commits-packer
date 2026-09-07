# Automated Test Plan

## 1. Test strategy

Use Python's standard-library `unittest` framework. Combine focused unit tests for configuration/bucketing with subprocess-level integration tests against a real disposable Git repository.

The suite must run from the repository root with:

```text
python -m unittest discover automated_test
```

No test may depend on the developer's global Git name, email, default branch, date format, or timezone.

## 2. Dedicated fixture

The fixture path is `automated_test/repository`, and `.env.testing` points to it using a path relative to that environment file. Before any deletion or recreation, resolve the path and verify that its parent is the repository's own `automated_test` directory.

Fixture lifecycle:

1. If the directory is absent, is not a Git worktree, or lacks local branch `master`, remove only that validated fixture path and initialize a new repository with `git init --initial-branch=master`.
2. Configure fixture-local author and committer identity.
3. Create 30 linear commits: three commits per calendar day at `00:00`, `08:00`, and `16:00` for ten consecutive days. Set both author and committer dates explicitly and update a tracked file with deterministic content in each commit.
4. If a valid fixture already exists, record its entry branch, check out `master`, and delete the recorded branch only when it is not `master`.
5. Capture the `master` object ID before invoking the utility.

The testing configuration uses a three-day retention window, a density of two old commits per day, and `FORCE=true`. The branch-tip cutoff anchor makes assertions stable even when a fixture is reused later.

## 3. Unit scenarios

### Configuration

- Load all recognized values from an environment file.
- Override each file value from the command line.
- Resolve file and CLI repository paths from their specified bases.
- Accept supported boolean spellings and reject other values.
- Reject missing required keys, negative retention, zero/negative density, malformed recognized lines, and an explicitly missing env file.
- Ignore unrelated environment keys.

### Bucket selection

- Select every commit at or after the exact cutoff, including equality.
- Select only the last graph-order commit in each occupied old bucket.
- Produce no more than `X` selected old commits for each author-local date.
- Handle `X=1`, `X=2`, and a non-divisor such as `X=5`.
- Respect each commit's recorded offset at local-date boundaries.
- Preserve graph ordering when author dates are non-monotonic.
- Always select the tip when retention is zero.

## 4. Primary integration scenario

Run the CLI from outside the target repository using `--env-file automated_test/.env.testing`. Assert:

- the process succeeds and checks out the current dated destination;
- `master` still resolves to its captured object ID;
- destination and source tips have different object IDs;
- destination and source tips resolve to identical tree IDs;
- the destination history is linear;
- every recent source commit has a corresponding recreated commit in graph order;
- old history has at most two selected commits per author-local day;
- each old selected commit corresponds to the final source commit in its occupied slot;
- recreated messages, author names/emails, author epochs, and author offsets match their selected sources;
- recreated committer metadata is not asserted equal to source committer metadata;
- output contains counts, cutoff, both branch names, and the SHA-change/history-sampling warning.

## 5. Safety and failure scenarios

- Reject a detached `HEAD` and an unborn branch without creating the destination.
- Reject a history containing a merge commit and leave every preexisting ref unchanged.
- Reject staged, unstaged, and untracked changes without force.
- With force, complete the rewrite while preserving dirty file contents and index state.
- When the destination exists, verify `n`, blank input, and EOF leave its ref unchanged; verify `y` replaces it.
- Verify force replaces an existing destination without reading stdin.
- Reject when the source branch already has the destination name.
- Reject a destination checked out in another linked worktree.
- Simulate a destination-ref race and verify compare-and-swap failure does not overwrite the competing value.
- Simulate commit construction failure and verify no visible ref changes.
- Simulate checkout failure after ref publication and verify the source stays checked out, the destination exists, and the diagnostic describes the partial outcome.
- Verify invalid repository paths, missing Git, and missing committer identity produce actionable errors without tracebacks.

## 6. Manual acceptance check

After the automated suite, inspect the fixture with:

```text
git -C automated_test/repository log --all --graph --format=fuller --decorate
```

Confirm visually that `master` retains all 30 original commits, the dated branch has reduced older density, recent commits are individually replayed, and both tips expose the same files.

