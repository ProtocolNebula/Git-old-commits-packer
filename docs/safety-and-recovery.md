# Safety and Recovery

## What the rewrite preserves

- The original local source branch and its original commit objects remain referenced.
- The destination tip contains exactly the same tree as the source tip.
- Every selected commit reuses its source tree, complete message, and author identity/date.
- Recent commits are represented individually and in their original graph order.

## What the rewrite does not preserve

- Original commit IDs are not preserved anywhere on the rewritten branch.
- Recent commits also receive new IDs because they are recreated with a different parent chain and new committer metadata.
- Old commits other than the final representative in each occupied bucket are omitted from the rewritten branch.
- Original committer identity/date, commit signatures, merge topology, mergetags, and unlisted commit headers are not copied.

The tool therefore creates a compact historical view, not a cryptographically or forensically equivalent archive.

## Before running

1. Verify that important local work is committed or backed up, even if force mode will be used.
2. Check for merge commits explicitly. The utility must reject them, but users should understand this limitation before selecting it as a backup strategy.
3. Fetch or otherwise confirm that the local source branch represents the history intended for rewriting.
4. Ensure the destination name is not used by an unrelated workflow or checked out in another worktree.
5. Keep the original branch until the rewritten result has been independently inspected.

Force mode is an acknowledgement mechanism. It must never run `reset`, `clean`, or an equivalent destructive checkout.

## Failure boundaries

Commit objects are built before a branch ref is published. If parsing or construction fails, the repository may contain unreachable objects but no visible ref should move; normal Git maintenance can eventually prune those objects.

The only intentionally mutable ref is `refs/heads/squashed/YYYY_MM_DD`. Its update is protected by the previously observed value, preventing silent overwrite if another process changes it concurrently.

Checkout happens after ref publication and cannot be part of the same atomic operation. A checkout failure can therefore leave the destination branch created while the source remains checked out. This is a safe, reportable partial result: neither ref needs to be deleted automatically.

## Comparing the result

Users should compare the source and destination before relying on the compact branch:

```text
git diff <source-branch> squashed/YYYY_MM_DD
git log --graph --decorate --oneline --all
git rev-parse <source-branch>^{tree}
git rev-parse squashed/YYYY_MM_DD^{tree}
```

The diff should be empty and both tree IDs should match. Commit IDs are expected to differ.

## Recovery

If the result is unsuitable, switch back to the original branch. That ref is the primary recovery path and must remain unchanged.

If an existing dated destination was replaced, its reflog may retain the earlier value:

```text
git reflog show squashed/YYYY_MM_DD
```

Reflog retention is not permanent, so it must not be treated as the only backup. Restoring a prior destination, deleting a destination, pushing rewritten history, or pruning objects should remain explicit user actions outside the utility.

