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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence, TextIO

try:
    from dotenv import dotenv_values
except ImportError as exc:  # pragma: no cover - exercised only before installation
    raise SystemExit("python-dotenv is required; install dependencies with: python -m pip install -r requirements.txt") from exc


KNOWN_ENV_KEYS = {
    "REPOSITORY_PATH",
    "UNSQUASHED_DAYS",
    "MAX_COMMITS_PER_DAY",
    "FORCE",
    "FORCE_RECHECK_ALL",
}
TRUE_VALUES = {"true", "1", "yes"}
FALSE_VALUES = {"false", "0", "no"}
GIT_REPOSITORY_ENV_KEYS = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
}
AUTHOR_LINE_RE = re.compile(rb"(.*) <([^<>]*)> (-?\d+) ([+-]\d{4})$")
ZERO_OID_BY_FORMAT = {"sha1": "0" * 40, "sha256": "0" * 64}
PROGRESS_INTERVAL_SECONDS = 2.0


class AppError(Exception):
    """An expected, user-facing failure."""


class ConfigError(AppError):
    pass


class GitError(AppError):
    pass


class UserDeclined(AppError):
    pass


class AlreadyCompressedHistory(AppError):
    """Normal finish: no newer commits require a rewrite."""


@dataclass(frozen=True)
class Config:
    repository_path: Path
    unsquashed_days: int
    max_commits_per_day: int
    force: bool
    force_recheck_all: bool
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
    preserved_prefix_count: int = 0
    finish_rule_date: str | None = None


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

    command = ["git", "-c", f"safe.directory={repository}", *args]
    process_env = os.environ.copy()
    for key in GIT_REPOSITORY_ENV_KEYS:
        process_env.pop(key, None)
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
    for key in GIT_REPOSITORY_ENV_KEYS:
        process_env.pop(key, None)
    process_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={repository}", *args],
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
    parser.add_argument(
        "--force-recheck-all",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="allow processing histories with more than seven consecutive already-compressed days",
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
    if namespace.force_recheck_all is not None:
        force_recheck_all = bool(namespace.force_recheck_all)
    elif "FORCE_RECHECK_ALL" in file_values:
        force_recheck_all = parse_bool(file_values["FORCE_RECHECK_ALL"], "FORCE_RECHECK_ALL")
    else:
        force_recheck_all = False
    return Config(repository_path, days, density, force, force_recheck_all, env_file)


def parse_author_line(value: bytes) -> tuple[str, str, int, str]:
    match = AUTHOR_LINE_RE.fullmatch(value)
    if match is None:
        raise GitError("could not parse an author header from a commit")
    name = match.group(1).decode("utf-8", errors="surrogateescape")
    email = match.group(2).decode("utf-8", errors="surrogateescape")
    epoch = int(match.group(3))
    offset = match.group(4).decode("ascii")
    return name, email, epoch, offset


def parse_commit_object(oid: str, raw: bytes) -> SourceCommit:
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


def parse_batch_commits(output: bytes, expected_oids: Sequence[str]) -> list[SourceCommit]:
    commits: list[SourceCommit] = []
    position = 0
    for expected_oid in expected_oids:
        header_end = output.find(b"\n", position)
        if header_end < 0:
            raise GitError(f"git cat-file --batch returned no header for commit {expected_oid}")
        header = output[position:header_end]
        position = header_end + 1
        if header.endswith(b" missing"):
            raise GitError(f"git cat-file --batch could not find commit {expected_oid}")
        pieces = header.split()
        if len(pieces) != 3:
            raise GitError(f"unexpected git cat-file --batch header for commit {expected_oid}")
        object_oid, object_type, size_value = pieces
        if object_oid.decode("ascii", errors="replace") != expected_oid:
            raise GitError(
                f"git cat-file --batch returned commit {object_oid.decode('ascii', errors='replace')} "
                f"while {expected_oid} was expected"
            )
        if object_type != b"commit":
            raise GitError(f"object {expected_oid} is not a commit")
        try:
            object_size = int(size_value)
        except ValueError as exc:
            raise GitError(f"invalid git cat-file --batch size for commit {expected_oid}") from exc
        content_end = position + object_size
        if content_end > len(output):
            raise GitError(f"truncated git cat-file --batch content for commit {expected_oid}")
        raw_commit = output[position:content_end]
        position = content_end
        if output[position : position + 1] != b"\n":
            raise GitError(f"missing git cat-file --batch terminator for commit {expected_oid}")
        position += 1
        commits.append(parse_commit_object(expected_oid, raw_commit))
    if position != len(output):
        raise GitError("git cat-file --batch returned unexpected trailing data")
    return commits


def read_commits_batch(repository: Path, oids: Sequence[str]) -> list[SourceCommit]:
    if not oids:
        return []
    requests = b"".join(oid.encode("ascii") + b"\n" for oid in oids)
    output = run_git(repository, ["cat-file", "--batch"], input_data=requests)
    return parse_batch_commits(output, oids)


def read_linear_history(repository: Path, source_oid: str) -> list[SourceCommit]:
    print("Progress: discovering source commits...", flush=True)
    raw = run_git(repository, ["rev-list", "--reverse", "--parents", source_oid])
    oids: list[str] = []
    for line in raw.splitlines():
        pieces = line.split()
        if len(pieces) > 2:
            raise AppError("history is not linear: a merge commit is reachable from the checked-out branch")
        if len(pieces) not in {1, 2}:
            raise GitError("unexpected parent data while reading linear history")
        oids.append(pieces[0].decode("ascii"))
    if not oids:
        raise AppError("checked-out branch has no commits")
    print(f"Progress: source commits detected: {len(oids)}; cursor 0/{len(oids)}", flush=True)
    commits = read_commits_batch(repository, oids)
    cursor = commits[-1].oid[:12] if commits else "none"
    print(f"Progress: source commits loaded: {len(commits)}/{len(oids)}; cursor {cursor}", flush=True)
    return commits


def parse_offset_minutes(offset: str) -> int:
    sign = 1 if offset[0] == "+" else -1
    return sign * (int(offset[1:3]) * 60 + int(offset[3:5]))


def bucket_for(commit: SourceCommit, max_commits_per_day: int) -> BucketKey:
    local = author_local_datetime(commit)
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    slot = min(max_commits_per_day - 1, seconds * max_commits_per_day // 86400)
    return BucketKey(local.date().isoformat(), slot)


def author_local_datetime(commit: SourceCommit) -> datetime:
    offset = timezone(timedelta(minutes=parse_offset_minutes(commit.author_offset)))
    return datetime.fromtimestamp(commit.author_epoch, tz=offset)


def already_compressed_dates(
    commits: Sequence[SourceCommit], cutoff: int, max_commits_per_day: int
) -> set[str]:
    counts: dict[str, int] = {}
    for commit in commits:
        if commit.author_epoch < cutoff:
            date = author_local_datetime(commit).date().isoformat()
            counts[date] = counts.get(date, 0) + 1
    return {date for date, count in counts.items() if count <= max_commits_per_day}


def longest_consecutive_date_run(dates: set[str]) -> tuple[int, str | None, str | None]:
    if not dates:
        return 0, None, None
    ordered = sorted(datetime.strptime(value, "%Y-%m-%d").date() for value in dates)
    longest = 1
    longest_start = ordered[0]
    longest_end = ordered[0]
    run_start = ordered[0]
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + timedelta(days=1):
            continue
        if previous - run_start + timedelta(days=1) > longest_end - longest_start + timedelta(days=1):
            longest = (previous - run_start).days + 1
            longest_start, longest_end = run_start, previous
        run_start = current
    final_length = (ordered[-1] - run_start).days + 1
    if final_length > longest:
        longest = final_length
        longest_start, longest_end = run_start, ordered[-1]
    return longest, longest_start.isoformat(), longest_end.isoformat()


def recompression_boundary(
    commits: Sequence[SourceCommit], cutoff: int, max_commits_per_day: int, force_recheck_all: bool
) -> tuple[int | None, str | None]:
    """Return the source index through which an existing compressed prefix is retained.

    Dates are inspected newest-first.  Once eight adjacent old dates are already at
    or below the target density, the newest date in that run is the boundary: all
    source commits on that date and earlier are already compressed and can remain
    as the destination's unchanged parent chain.
    """

    if force_recheck_all:
        return None, None
    compressed_dates = already_compressed_dates(commits, cutoff, max_commits_per_day)
    if not compressed_dates:
        return None, None
    ordered = sorted(
        (datetime.strptime(value, "%Y-%m-%d").date() for value in compressed_dates),
        reverse=True,
    )
    run_newest = ordered[0]
    run_length = 1
    previous = ordered[0]
    for current in ordered[1:]:
        if previous - current == timedelta(days=1):
            run_length += 1
        else:
            run_newest = current
            run_length = 1
        if run_length > 7:
            boundary_date = run_newest.isoformat()
            candidates = [
                index
                for index, commit in enumerate(commits)
                if commit.author_epoch < cutoff
                and author_local_datetime(commit).date().isoformat() <= boundary_date
            ]
            if candidates:
                return max(candidates), boundary_date
            return None, boundary_date
        previous = current
    return None, None


def select_indices(
    commits: Sequence[SourceCommit],
    unsquashed_days: int,
    max_commits_per_day: int,
    force_recheck_all: bool = False,
) -> tuple[list[int], int, int, int]:
    if not commits:
        raise ValueError("at least one commit is required")
    cutoff = commits[-1].author_epoch - unsquashed_days * 86400
    selected: set[int] = set()
    last_old_by_bucket: dict[BucketKey, int] = {}
    old_indices_by_date: dict[str, list[int]] = {}
    recent_count = 0
    for index, commit in enumerate(commits):
        if commit.author_epoch >= cutoff:
            selected.add(index)
            recent_count += 1
        else:
            date = author_local_datetime(commit).date().isoformat()
            old_indices_by_date.setdefault(date, []).append(index)

    preserved_prefix_index, _ = recompression_boundary(
        commits, cutoff, max_commits_per_day, force_recheck_all
    )
    compressed_dates = already_compressed_dates(commits, cutoff, max_commits_per_day)

    for date, indices in old_indices_by_date.items():
        if date in compressed_dates:
            selected.update(indices)
            continue
        for index in indices:
            last_old_by_bucket[bucket_for(commits[index], max_commits_per_day)] = index
    selected.update(last_old_by_bucket.values())
    if preserved_prefix_index is not None:
        selected = {index for index in selected if index > preserved_prefix_index}
    old_selected_count = sum(1 for index in selected if commits[index].author_epoch < cutoff)
    return sorted(selected), cutoff, recent_count, old_selected_count


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
    current_worktree: Path | None = None
    current_repository = repository.resolve()
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_worktree = Path(line[9:]).resolve()
        elif line.strip() == f"branch refs/heads/{destination}":
            if current_worktree is not None and current_worktree != current_repository:
                return True
    return False


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


def create_rewritten_commits(
    repository: Path,
    commits: Sequence[SourceCommit],
    selected: Sequence[int],
    base_parent: str | None = None,
) -> list[str]:
    rewritten: list[str] = []
    parent = base_parent
    total = len(selected)
    last_report = time.monotonic()
    print(f"Progress: recreating commits: 0/{total}; source cursor 0/{len(commits)}", flush=True)
    for position, index in enumerate(selected, start=1):
        commit = commits[index]
        environment = os.environ.copy()
        for key in GIT_REPOSITORY_ENV_KEYS:
            if key in environment:
                del environment[key]
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment.update(
            {
                "GIT_AUTHOR_NAME": commit.author_name,
                "GIT_AUTHOR_EMAIL": commit.author_email,
                "GIT_AUTHOR_DATE": f"@{commit.author_epoch} {commit.author_offset}",
            }
        )
        args = ["commit-tree", commit.tree]
        if parent is not None:
            args.extend(["-p", parent])
        oid = _decode(run_git(repository, args, input_data=commit.message, env=environment))
        if not oid:
            raise GitError("git commit-tree returned an empty object ID")
        rewritten.append(oid)
        parent = oid
        now = time.monotonic()
        if now - last_report >= PROGRESS_INTERVAL_SECONDS or position == total:
            print(
                f"Progress: recreating commits: {position}/{total}; "
                f"source cursor {index + 1}/{len(commits)} ({commit.oid[:12]})",
                flush=True,
            )
            last_report = now
    return rewritten


def publish_destination(
    repository: Path,
    destination: str,
    rewritten_oid: str,
    old_destination_oid: str | None,
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


def rewrite(config: Config) -> RewriteResult:
    repository = ensure_worktree(config.repository_path)
    branch = source_branch(repository)
    original_oid = source_oid(repository)
    ensure_clean(repository, config.force)
    ensure_committer_identity(repository)
    commits = read_linear_history(repository, original_oid)
    cutoff = commits[-1].author_epoch - config.unsquashed_days * 86400
    preserved_prefix_index, finish_rule_date = recompression_boundary(
        commits, cutoff, config.max_commits_per_day, config.force_recheck_all
    )
    selected, cutoff, recent_count, old_count = select_indices(
        commits, config.unsquashed_days, config.max_commits_per_day, config.force_recheck_all
    )
    if not selected and preserved_prefix_index is not None:
        raise AlreadyCompressedHistory(
            "finish rule reached already-compressed history "
            f"through {finish_rule_date}; no newer commits require rewriting"
    )
    date_name = datetime.now().astimezone().strftime("%Y_%m_%d")
    destination = f"squashed/{date_name}"
    if destination == branch:
        raise AppError(
            "destination branch is the checked-out source branch; rename or switch the source branch before rerunning"
        )
    if destination_checked_out_elsewhere(repository, destination):
        raise AppError(f"destination branch is checked out in another worktree: {destination}")
    old_destination_oid = resolve_destination(repository, destination)
    if old_destination_oid is not None:
        confirm_replacement(destination, config.force)

    base_parent = commits[preserved_prefix_index].oid if preserved_prefix_index is not None else None
    rewritten_oids = create_rewritten_commits(repository, commits, selected, base_parent)
    if not rewritten_oids:
        raise AppError("no commits were selected for rewriting")
    current_source_oid = _decode(run_git(repository, ["rev-parse", f"refs/heads/{branch}"]))
    if current_source_oid != original_oid:
        raise AppError("source branch changed while commits were being constructed; no destination ref was updated")
    publish_destination(repository, destination, rewritten_oids[-1], old_destination_oid)
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
        (preserved_prefix_index + 1) if preserved_prefix_index is not None else 0,
        finish_rule_date,
    )


def print_success(result: RewriteResult) -> None:
    cutoff = datetime.fromtimestamp(result.cutoff_epoch, tz=timezone.utc).isoformat()
    print(f"Source branch: {result.source_branch}")
    print(f"Destination branch: {result.destination_branch}")
    print(f"Source commits: {result.source_count}")
    print(f"Recreated commits: {result.rewritten_count}")
    if result.preserved_prefix_count:
        print(f"Existing compressed prefix retained: {result.preserved_prefix_count}")
    print(f"Recent commits replayed individually: {result.recent_count}")
    print(f"Old-history commits selected: {result.old_representative_count}")
    print(f"Cutoff (UTC): {cutoff}")
    print(f"Original source tip retained: {result.source_oid}")
    print(f"Rewritten destination tip: {result.rewritten_oid}")
    print(f"Source branch remains checked out: {result.source_branch}")
    if result.finish_rule_date is not None:
        print(
            "Finish rule: stopped re-compressing already-compressed history "
            f"through {result.finish_rule_date}."
        )
    print("Warning: recreated commits have new SHAs; older history was sampled into time buckets.")


def print_timing(started_at: datetime, started_monotonic: float, stream: TextIO = sys.stdout) -> None:
    finished_at = datetime.now().astimezone()
    elapsed = max(0.0, time.monotonic() - started_monotonic)
    print(f"Start time: {started_at.isoformat(timespec='seconds')}", file=stream)
    print(f"End time: {finished_at.isoformat(timespec='seconds')}", file=stream)
    print(f"Elapsed time: {elapsed:.3f} seconds", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    started_at = datetime.now().astimezone()
    started_monotonic = time.monotonic()
    try:
        config = load_config(argv)
        result = rewrite(config)
    except UserDeclined as exc:
        print(str(exc))
        print("No changes made.")
        print_timing(started_at, started_monotonic)
        return 0
    except AlreadyCompressedHistory as exc:
        print(f"Finish rule: {exc}")
        print("No changes made.")
        print_timing(started_at, started_monotonic)
        return 0
    except (AppError, GitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print_timing(started_at, started_monotonic, sys.stderr)
        return 1
    print_success(result)
    print_timing(started_at, started_monotonic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
