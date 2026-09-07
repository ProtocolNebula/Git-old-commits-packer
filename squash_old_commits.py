#!/usr/bin/env python3
"""Create a compact, rewritten view of a linear Git branch.

The source branch is never moved.  Rewritten commits are first created as
unreachable objects and are published under ``squashed/YYYY_MM_DD`` only after
all validation and object construction has succeeded.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

try:
    from dotenv import dotenv_values
except ImportError as exc:  # pragma: no cover - exercised only before installation
    raise SystemExit("python-dotenv is required; install dependencies with: python -m pip install -r requirements.txt") from exc


KNOWN_ENV_KEYS = {
    "REPOSITORY_PATH",
    "UNSQUASHED_DAYS",
    "MAX_COMMITS_PER_DAY",
    "FORCE",
}
TRUE_VALUES = {"true", "1", "yes"}
FALSE_VALUES = {"false", "0", "no"}
AUTHOR_LINE_RE = re.compile(rb"(.*) <([^<>]*)> (-?\d+) ([+-]\d{4})$")
ZERO_OID_BY_FORMAT = {"sha1": "0" * 40, "sha256": "0" * 64}


class AppError(Exception):
    """An expected, user-facing failure."""


class ConfigError(AppError):
    pass


class GitError(AppError):
    pass


class UserDeclined(AppError):
    pass


class DestinationCheckoutError(AppError):
    def __init__(self, destination: str, source: str, detail: str) -> None:
        super().__init__(detail)
        self.destination = destination
        self.source = source


@dataclass(frozen=True)
class Config:
    repository_path: Path
    unsquashed_days: int
    max_commits_per_day: int
    force: bool
    env_file: Path


@dataclass(frozen=True)
class SourceCommit:
    oid: str
    tree: str
    message: bytes
    author_name: str
    author_email: str
    author_epoch: int
    author_offset: str


@dataclass(frozen=True)
class BucketKey:
    local_date: str
    slot: int


@dataclass(frozen=True)
class RewriteResult:
    source_branch: str
    destination_branch: str
    source_oid: str
    rewritten_oid: str
    source_count: int
    rewritten_count: int
    recent_count: int
    old_representative_count: int
    cutoff_epoch: int


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def run_git(
    repository: Path,
    args: Sequence[str],
    *,
    input_data: bytes | None = None,
    env: Mapping[str, str] | None = None,
    remove_identity_env: bool = False,
) -> bytes:
    """Run Git without a shell and raise a concise error on failure."""

    command = ["git", *args]
    process_env = os.environ.copy()
    process_env["GIT_TERMINAL_PROMPT"] = "0"
    if remove_identity_env:
        for key in list(process_env):
            if key.startswith("GIT_AUTHOR_") or key.startswith("GIT_COMMITTER_"):
                del process_env[key]
    if env is not None:
        process_env.update(env)
    try:
        completed = subprocess.run(
            command,
            cwd=str(repository),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"could not execute Git: {exc}") from exc
    if completed.returncode != 0:
        detail = _decode(completed.stderr) or _decode(completed.stdout)
        rendered = " ".join(command)
        raise GitError(f"Git command failed ({rendered}): {detail}")
    return completed.stdout


def run_git_optional(repository: Path, args: Sequence[str]) -> tuple[int, bytes, bytes]:
    """Run Git while allowing callers to inspect a nonzero status."""

    process_env = os.environ.copy()
    process_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repository),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise GitError(f"could not execute Git: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


def parse_env_file(path: Path, *, required: bool) -> dict[str, str]:
    """Load recognized settings with python-dotenv without mutating os.environ."""

    if not path.exists():
        if required:
            raise ConfigError(f"environment file does not exist: {path}")
        return {}
    if not path.is_file():
        raise ConfigError(f"environment path is not a file: {path}")

    values: dict[str, str] = {}
    try:
        parsed = dotenv_values(dotenv_path=path, encoding="utf-8", interpolate=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigError(f"cannot parse environment file {path}: {exc}") from exc
    for key, value in parsed.items():
        if key in KNOWN_ENV_KEYS:
            if value is None:
                raise ConfigError(f"environment key has no value: {path}:{key}")
            values[key] = value
    return values


def parse_bool(value: str, source: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigError(f"invalid boolean for {source}: {value!r}")


def parse_nonnegative_int(value: str, source: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ConfigError(f"invalid integer for {source}: {value!r}") from exc
    if parsed < 0:
        raise ConfigError(f"{source} must be nonnegative")
    return parsed


def parse_positive_int(value: str, source: str) -> int:
    parsed = parse_nonnegative_int(value, source)
    if parsed == 0:
        raise ConfigError(f"{source} must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite a checked-out linear Git branch into "
            "squashed/YYYY_MM_DD. Original commit IDs are not preserved."
        )
    )
    parser.add_argument("--env-file", type=Path, help="configuration file (default: .env beside this script)")
    parser.add_argument("--repository-path", type=Path)
    parser.add_argument("--unsquashed-days", type=int)
    parser.add_argument("--max-commits-per-day", type=int)
    parser.add_argument(
        "--force",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="allow dirty worktrees and replace an existing destination (use --no-force to override .env)",
    )
    return parser


def load_config(argv: Sequence[str] | None = None) -> Config:
    parser = build_parser()
    namespace = parser.parse_args(argv)
    script_dir = Path(__file__).resolve().parent
    explicit_env_file = namespace.env_file is not None
    env_file = (namespace.env_file or (script_dir / ".env")).expanduser()
    if not env_file.is_absolute():
        env_file = (Path.cwd() / env_file).resolve()
    file_values = parse_env_file(env_file, required=explicit_env_file)

    repository_value = (
        str(namespace.repository_path)
        if namespace.repository_path is not None
        else file_values.get("REPOSITORY_PATH")
    )
    if repository_value is None or not repository_value.strip():
        parser.error("repository path is required via --repository-path or REPOSITORY_PATH")
    repository_path = Path(repository_value).expanduser()
    if namespace.repository_path is None and not repository_path.is_absolute():
        repository_path = (env_file.parent / repository_path).resolve()
    else:
        repository_path = repository_path.resolve()

    days_value = (
        str(namespace.unsquashed_days)
        if namespace.unsquashed_days is not None
        else file_values.get("UNSQUASHED_DAYS")
    )
    density_value = (
        str(namespace.max_commits_per_day)
        if namespace.max_commits_per_day is not None
        else file_values.get("MAX_COMMITS_PER_DAY")
    )
    if days_value is None:
        parser.error("unsquashed days is required via --unsquashed-days or UNSQUASHED_DAYS")
    if density_value is None:
        parser.error("maximum commits per day is required via --max-commits-per-day or MAX_COMMITS_PER_DAY")

    days = parse_nonnegative_int(days_value, "UNSQUASHED_DAYS")
    density = parse_positive_int(density_value, "MAX_COMMITS_PER_DAY")
    if namespace.force is not None:
        force = bool(namespace.force)
    elif "FORCE" in file_values:
        force = parse_bool(file_values["FORCE"], "FORCE")
    else:
        force = False
    return Config(repository_path, days, density, force, env_file)


def parse_author_line(value: bytes) -> tuple[str, str, int, str]:
    match = AUTHOR_LINE_RE.fullmatch(value)
    if match is None:
        raise GitError("could not parse an author header from a commit")
    name = match.group(1).decode("utf-8", errors="surrogateescape")
    email = match.group(2).decode("utf-8", errors="surrogateescape")
    epoch = int(match.group(3))
    offset = match.group(4).decode("ascii")
    return name, email, epoch, offset


def read_commit(repository: Path, oid: str) -> SourceCommit:
    raw = run_git(repository, ["cat-file", "commit", oid])
    separator = raw.find(b"\n\n")
    if separator < 0:
        raise GitError(f"commit {oid} has no message separator")
    tree: str | None = None
    author: bytes | None = None
    for header in raw[:separator].splitlines():
        if header.startswith(b"tree ") and tree is None:
            tree = header[5:].decode("ascii")
        elif header.startswith(b"author ") and author is None:
            author = header[7:]
    if tree is None or author is None:
        raise GitError(f"commit {oid} is missing its tree or author header")
    name, email, epoch, offset = parse_author_line(author)
    return SourceCommit(oid, tree, raw[separator + 2 :], name, email, epoch, offset)


def read_linear_history(repository: Path, source_oid: str) -> list[SourceCommit]:
    raw = run_git(repository, ["rev-list", "--reverse", "--parents", source_oid])
    commits: list[SourceCommit] = []
    for line in raw.splitlines():
        pieces = line.split()
        if len(pieces) > 2:
            raise AppError("history is not linear: a merge commit is reachable from the checked-out branch")
        if len(pieces) not in {1, 2}:
            raise GitError("unexpected parent data while reading linear history")
        commits.append(read_commit(repository, pieces[0].decode("ascii")))
    if not commits:
        raise AppError("checked-out branch has no commits")
    return commits


def parse_offset_minutes(offset: str) -> int:
    sign = 1 if offset[0] == "+" else -1
    return sign * (int(offset[1:3]) * 60 + int(offset[3:5]))


def bucket_for(commit: SourceCommit, max_commits_per_day: int) -> BucketKey:
    offset = timezone(timedelta(minutes=parse_offset_minutes(commit.author_offset)))
    local = datetime.fromtimestamp(commit.author_epoch, tz=offset)
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    slot = min(max_commits_per_day - 1, seconds * max_commits_per_day // 86400)
    return BucketKey(local.date().isoformat(), slot)


def select_indices(
    commits: Sequence[SourceCommit], unsquashed_days: int, max_commits_per_day: int
) -> tuple[list[int], int, int, int]:
    if not commits:
        raise ValueError("at least one commit is required")
    cutoff = commits[-1].author_epoch - unsquashed_days * 86400
    selected: set[int] = set()
    last_old_by_bucket: dict[BucketKey, int] = {}
    recent_count = 0
    for index, commit in enumerate(commits):
        if commit.author_epoch >= cutoff:
            selected.add(index)
            recent_count += 1
        else:
            last_old_by_bucket[bucket_for(commit, max_commits_per_day)] = index
    selected.update(last_old_by_bucket.values())
    return sorted(selected), cutoff, recent_count, len(last_old_by_bucket)


def source_branch(repository: Path) -> str:
    try:
        return _decode(run_git(repository, ["symbolic-ref", "--quiet", "--short", "HEAD"]))
    except GitError as exc:
        raise AppError("repository must have a checked-out named branch (detached HEAD is not supported)") from exc


def source_oid(repository: Path) -> str:
    try:
        return _decode(run_git(repository, ["rev-parse", "--verify", "HEAD^{commit}"]))
    except GitError as exc:
        raise AppError("checked-out branch is unborn or has no commit") from exc


def ensure_worktree(repository_path: Path) -> Path:
    if not repository_path.exists() or not repository_path.is_dir():
        raise AppError(f"repository path is not a directory: {repository_path}")
    try:
        root = _decode(run_git(repository_path, ["rev-parse", "--show-toplevel"]))
    except GitError as exc:
        raise AppError(f"not a Git worktree: {repository_path}") from exc
    return Path(root).resolve()


def ensure_clean(repository: Path, force: bool) -> None:
    status = run_git(repository, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if status and not force:
        raise AppError("working tree has tracked or untracked changes; commit/stash them or use --force")


def ensure_committer_identity(repository: Path) -> None:
    try:
        run_git(repository, ["var", "GIT_COMMITTER_IDENT"], remove_identity_env=True)
    except GitError as exc:
        raise AppError("Git has no usable committer identity; configure user.name and user.email first") from exc


def destination_checked_out_elsewhere(repository: Path, destination: str) -> bool:
    output = run_git(repository, ["worktree", "list", "--porcelain"]).decode("utf-8", errors="replace")
    return any(line.strip() == f"branch refs/heads/{destination}" for line in output.splitlines())


def resolve_destination(repository: Path, destination: str) -> str | None:
    status, stdout, _ = run_git_optional(repository, ["rev-parse", "--verify", "--quiet", f"refs/heads/{destination}"])
    if status == 0:
        return _decode(stdout)
    return None


def confirm_replacement(destination: str, force: bool) -> None:
    if force:
        return
    try:
        answer = input(f"Recreate {destination}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        raise UserDeclined(f"leaving existing destination unchanged: {destination}")


def object_format(repository: Path) -> str:
    value = _decode(run_git(repository, ["rev-parse", "--show-object-format"]))
    if value not in ZERO_OID_BY_FORMAT:
        raise AppError(f"unsupported Git object format: {value}")
    return value


def create_rewritten_commits(repository: Path, commits: Sequence[SourceCommit], selected: Sequence[int]) -> list[str]:
    rewritten: list[str] = []
    for index in selected:
        commit = commits[index]
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        for key in list(environment):
            if key.startswith("GIT_AUTHOR_") or key.startswith("GIT_COMMITTER_"):
                del environment[key]
        environment.update(
            {
                "GIT_AUTHOR_NAME": commit.author_name,
                "GIT_AUTHOR_EMAIL": commit.author_email,
                "GIT_AUTHOR_DATE": f"@{commit.author_epoch} {commit.author_offset}",
            }
        )
        args = ["commit-tree", commit.tree]
        if rewritten:
            args.extend(["-p", rewritten[-1]])
        oid = _decode(run_git(repository, args, input_data=commit.message, env=environment))
        if not oid:
            raise GitError("git commit-tree returned an empty object ID")
        rewritten.append(oid)
    return rewritten


def publish_and_checkout(
    repository: Path,
    destination: str,
    rewritten_oid: str,
    old_destination_oid: str | None,
    source_branch_name: str,
) -> None:
    object_format_name = object_format(repository)
    expected_old = old_destination_oid or ZERO_OID_BY_FORMAT[object_format_name]
    run_git(
        repository,
        [
            "update-ref",
            f"refs/heads/{destination}",
            rewritten_oid,
            expected_old,
            "-m",
            "squash old commits",
        ],
    )
    try:
        run_git(repository, ["switch", destination])
    except GitError as exc:
        raise DestinationCheckoutError(
            destination,
            source_branch_name,
            f"destination ref was created, but checkout failed: {exc}",
        ) from exc


def rewrite(config: Config) -> RewriteResult:
    repository = ensure_worktree(config.repository_path)
    branch = source_branch(repository)
    original_oid = source_oid(repository)
    ensure_clean(repository, config.force)
    ensure_committer_identity(repository)
    commits = read_linear_history(repository, original_oid)
    selected, cutoff, recent_count, old_count = select_indices(
        commits, config.unsquashed_days, config.max_commits_per_day
    )
    date_name = datetime.now().astimezone().strftime("%Y_%m_%d")
    destination = f"squashed/{date_name}"
    if branch == destination:
        raise AppError("the checked-out source branch is already the destination branch")
    if destination_checked_out_elsewhere(repository, destination):
        raise AppError(f"destination branch is checked out in another worktree: {destination}")
    old_destination_oid = resolve_destination(repository, destination)
    if old_destination_oid is not None:
        confirm_replacement(destination, config.force)

    rewritten_oids = create_rewritten_commits(repository, commits, selected)
    if not rewritten_oids:
        raise AppError("no commits were selected for rewriting")
    current_source_oid = _decode(run_git(repository, ["rev-parse", f"refs/heads/{branch}"]))
    if current_source_oid != original_oid:
        raise AppError("source branch changed while commits were being constructed; no destination ref was updated")
    publish_and_checkout(repository, destination, rewritten_oids[-1], old_destination_oid, branch)
    return RewriteResult(
        branch,
        destination,
        original_oid,
        rewritten_oids[-1],
        len(commits),
        len(rewritten_oids),
        recent_count,
        old_count,
        cutoff,
    )


def print_success(result: RewriteResult) -> None:
    cutoff = datetime.fromtimestamp(result.cutoff_epoch, tz=timezone.utc).isoformat()
    print(f"Source branch: {result.source_branch}")
    print(f"Destination branch: {result.destination_branch}")
    print(f"Source commits: {result.source_count}")
    print(f"Recreated commits: {result.rewritten_count}")
    print(f"Recent commits replayed individually: {result.recent_count}")
    print(f"Old-history representatives: {result.old_representative_count}")
    print(f"Cutoff (UTC): {cutoff}")
    print(f"Original source tip retained: {result.source_oid}")
    print(f"Rewritten destination tip: {result.rewritten_oid}")
    print("Warning: recreated commits have new SHAs; older history was sampled into time buckets.")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = load_config(argv)
        result = rewrite(config)
    except UserDeclined as exc:
        print(str(exc))
        print("No changes made.")
        return 0
    except DestinationCheckoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"Source branch remains checked out: {exc.source}", file=sys.stderr)
        return 1
    except (AppError, GitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print_success(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
