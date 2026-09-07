from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "squash_old_commits.py"
AUTOMATED_TEST_ROOT = Path(__file__).resolve().parent
FIXTURE = AUTOMATED_TEST_ROOT / "repository"
ENV_FILE = AUTOMATED_TEST_ROOT / ".env.testing"

sys.path.insert(0, str(ROOT))
import squash_old_commits as squash  # noqa: E402


def git(path: Path, *args: str, input_data: bytes | None = None, check: bool = True) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": "fixture-author@example.test",
            "GIT_COMMITTER_NAME": "Fixture Committer",
            "GIT_COMMITTER_EMAIL": "fixture-committer@example.test",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=str(path),
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.decode(errors="replace"))
    return completed.stdout.decode("utf-8", errors="replace").strip()


def fixture_is_valid() -> bool:
    if not FIXTURE.is_dir():
        return False
    result = subprocess.run(
        ["git", "-C", str(FIXTURE), "show-ref", "--verify", "--quiet", "refs/heads/master"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def recreate_fixture() -> None:
    resolved = FIXTURE.resolve()
    expected_parent = AUTOMATED_TEST_ROOT.resolve()
    if resolved.parent != expected_parent:
        raise AssertionError(f"refusing to remove unexpected fixture path: {resolved}")
    if FIXTURE.exists():
        def clear_readonly(function: object, path: str, _: object) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)  # type: ignore[operator]

        shutil.rmtree(FIXTURE, onerror=clear_readonly)
    FIXTURE.mkdir(parents=True)
    git(FIXTURE, "init", "--initial-branch=master")
    git(FIXTURE, "config", "user.name", "Fixture Committer")
    git(FIXTURE, "config", "user.email", "fixture-committer@example.test")
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for index in range(30):
        moment = start + timedelta(hours=8 * index)
        (FIXTURE / "history.txt").write_text(f"commit {index}\n", encoding="utf-8")
        git(FIXTURE, "add", "history.txt")
        date = moment.strftime("%Y-%m-%dT%H:%M:%S+0000")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Fixture Author",
                "GIT_AUTHOR_EMAIL": "fixture-author@example.test",
                "GIT_COMMITTER_NAME": "Fixture Committer",
                "GIT_COMMITTER_EMAIL": "fixture-committer@example.test",
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            }
        )
        subprocess.run(["git", "commit", "-m", f"fixture commit {index}"], cwd=FIXTURE, env=environment, check=True)


def prepare_fixture() -> None:
    if not fixture_is_valid():
        recreate_fixture()
        return
    entry_branch = git(FIXTURE, "branch", "--show-current")
    if entry_branch != "master":
        git(FIXTURE, "switch", "master")
        git(FIXTURE, "branch", "-D", entry_branch)


class SelectionTests(unittest.TestCase):
    def make_commits(self, epochs: list[int]) -> list[squash.SourceCommit]:
        return [
            squash.SourceCommit(str(index), "tree", f"message {index}".encode(), "A", "a@example.test", epoch, "+0000")
            for index, epoch in enumerate(epochs)
        ]

    def test_cutoff_and_last_bucket_representative(self) -> None:
        start = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        commits = self.make_commits([start + 8 * 3600 * index for index in range(6)])
        selected, cutoff, recent, old = squash.select_indices(commits, 1, 2)
        self.assertEqual(cutoff, commits[-1].author_epoch - 86400)
        self.assertEqual(selected, [1, 2, 3, 4, 5])
        self.assertEqual(recent, 4)
        self.assertEqual(old, 1)

    def test_non_divisor_density_stays_bounded(self) -> None:
        start = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
        commits = self.make_commits([start + 3600 * index for index in range(48)])
        selected, cutoff, _, _ = squash.select_indices(commits, 0, 5)
        counts: dict[str, int] = {}
        for index in selected:
            if commits[index].author_epoch < cutoff:
                key = squash.bucket_for(commits[index], 5).local_date
                counts[key] = counts.get(key, 0) + 1
        self.assertTrue(all(value <= 5 for value in counts.values()))


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        prepare_fixture()
        cls.original_tip = git(FIXTURE, "rev-parse", "master")
        cls.source_tree = git(FIXTURE, "rev-parse", "master^{tree}")

    def run_script(self, *extra: str, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.pop("GIT_AUTHOR_NAME", None)
        environment.pop("GIT_AUTHOR_EMAIL", None)
        environment.pop("GIT_COMMITTER_NAME", None)
        environment.pop("GIT_COMMITTER_EMAIL", None)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--env-file", str(ENV_FILE), *extra],
            cwd=str(ROOT),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )

    def test_rewrite_preserves_source_and_tree(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        destination = git(FIXTURE, "branch", "--show-current")
        self.assertTrue(destination.startswith("squashed/"))
        self.assertEqual(git(FIXTURE, "rev-parse", "master"), self.original_tip)
        self.assertNotEqual(git(FIXTURE, "rev-parse", destination), self.original_tip)
        self.assertEqual(git(FIXTURE, "rev-parse", f"{destination}^{{tree}}"), self.source_tree)
        self.assertIn(b"Original source tip retained", result.stdout)
        self.assertIn(b"new SHAs", result.stdout)

        count = int(git(FIXTURE, "rev-list", "--count", destination))
        self.assertEqual(count, 23)
        self.assertEqual(git(FIXTURE, "rev-list", "--merges", destination), "")

    def test_cli_overrides_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repo"
            repository.mkdir()
            git(repository, "init", "--initial-branch=master")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.test")
            (repository / "file.txt").write_text("one\n", encoding="utf-8")
            git(repository, "add", "file.txt")
            git(repository, "commit", "-m", "one")
            environment_file = Path(temporary) / ".env"
            environment_file.write_text(
                "REPOSITORY_PATH=repo\nUNSQUASHED_DAYS=99\nMAX_COMMITS_PER_DAY=99\nFORCE=true\n",
                encoding="utf-8",
            )
            config = squash.load_config(
                [
                    "--env-file",
                    str(environment_file),
                    "--unsquashed-days",
                    "0",
                    "--max-commits-per-day",
                    "1",
                    "--no-force",
                ]
            )
            self.assertEqual(config.repository_path, repository.resolve())
            self.assertEqual(config.unsquashed_days, 0)
            self.assertEqual(config.max_commits_per_day, 1)
            self.assertFalse(config.force)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repository-path",
                    str(repository),
                    "--unsquashed-days",
                    "0",
                    "--max-commits-per-day",
                    "1",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_dirty_tree_is_rejected_without_force(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = Path(temporary)
            git(repository, "init", "--initial-branch=master")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.test")
            (repository / "file.txt").write_text("one\n", encoding="utf-8")
            git(repository, "add", "file.txt")
            git(repository, "commit", "-m", "one")
            (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repository-path",
                    str(repository),
                    "--unsquashed-days",
                    "1",
                    "--max-commits-per-day",
                    "2",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"working tree", result.stderr)
            self.assertEqual(git(repository, "for-each-ref", "refs/heads/squashed/", "--format=%(refname)"), "")

    def test_merge_history_is_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            repository = Path(temporary)
            git(repository, "init", "--initial-branch=master")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.test")
            (repository / "file.txt").write_text("one\n", encoding="utf-8")
            git(repository, "add", "file.txt")
            git(repository, "commit", "-m", "one")
            git(repository, "switch", "-c", "feature")
            (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
            git(repository, "add", "feature.txt")
            git(repository, "commit", "-m", "feature")
            git(repository, "switch", "master")
            (repository / "master.txt").write_text("master\n", encoding="utf-8")
            git(repository, "add", "master.txt")
            git(repository, "commit", "-m", "master")
            git(repository, "merge", "--no-ff", "feature", "-m", "merge")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--repository-path",
                    str(repository),
                    "--unsquashed-days",
                    "1",
                    "--max-commits-per-day",
                    "2",
                ],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"not linear", result.stderr)


if __name__ == "__main__":
    unittest.main()
