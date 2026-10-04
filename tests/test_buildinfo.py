"""Which commit a stick records as its source."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from portlin import buildinfo


def test_the_environment_wins_so_a_container_without_git_still_knows(monkeypatch):
    monkeypatch.setenv(buildinfo.COMMIT_ENV, " 1a83e8c \n")
    assert buildinfo.source_commit() == "1a83e8c"


def test_an_installed_copy_reports_nothing_rather_than_a_neighbours_commit(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(buildinfo.COMMIT_ENV, raising=False)
    monkeypatch.setattr(buildinfo, "REPO", tmp_path)
    assert buildinfo.source_commit() == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
class TestCheckout:
    @pytest.fixture
    def repo(self, monkeypatch, tmp_path):
        monkeypatch.delenv(buildinfo.COMMIT_ENV, raising=False)
        monkeypatch.setattr(buildinfo, "REPO", tmp_path)

        def git(*args):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

        git("init", "-q")
        (tmp_path / "file").write_text("one\n")
        git("add", "file")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one")
        return tmp_path

    def test_a_clean_tree_is_its_short_hash(self, repo):
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert buildinfo.source_commit() == head

    def test_local_changes_are_marked_dirty(self, repo):
        (repo / "file").write_text("two\n")
        assert buildinfo.source_commit().endswith(buildinfo.DIRTY_SUFFIX)


def test_describe_leaves_out_an_unknown_commit():
    assert buildinfo.describe("0.1.2", "1a83e8c") == "0.1.2 (1a83e8c)"
    assert buildinfo.describe("0.1.2", "") == "0.1.2"
