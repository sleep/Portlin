"""zsh as the alternative shell: the themed ~/.zshrc run by a real zsh, and
portlin-shell, which switches an account between bash and zsh after setup.

The rc is exercised under zsh itself where one is installed, because the
failures that matter are runtime ones a text assertion cannot see: a parse
error that leaves every terminal without a prompt, or a git segment that
miscounts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import load_tool
from portlin import templates

needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="needs zsh")
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def run_zsh(tmp_path: Path, script: str, cwd: Path | None = None) -> str:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".zshrc").write_text(templates.render_zshrc())
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "ZDOTDIR": str(home), "TERM": "dumb",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(["zsh", "-i", "-c", script], cwd=cwd or home, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                   check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})


@needs_zsh
class TestZshrcUnderZsh:
    def test_it_parses(self, tmp_path):
        rc = tmp_path / ".zshrc"
        rc.write_text(templates.render_zshrc())
        subprocess.run(["zsh", "-n", str(rc)], check=True)

    def test_an_interactive_shell_starts_silently(self, tmp_path):
        assert run_zsh(tmp_path, "print ready") == "ready\n"

    def test_a_non_interactive_shell_reads_none_of_it(self, tmp_path):
        rc = tmp_path / ".zshrc"
        rc.write_text(templates.render_zshrc())
        result = subprocess.run(["zsh", "-c", f"source {rc}; print -r -- ${{+functions[_portlin_precmd]}}"],
                                capture_output=True, text=True, check=True)
        assert result.stdout == "0\n"

    def test_a_failed_slow_command_reports_status_and_time(self, tmp_path):
        out = run_zsh(tmp_path, "_portlin_started=$(( EPOCHREALTIME - 75 )); false; _portlin_precmd; "
                                "print -r -- $_portlin_status")
        assert out == "%F{3}exit 1%f %F{7}1m 15s%f\n"

    def test_a_signal_is_named_rather_than_numbered(self, tmp_path):
        out = run_zsh(tmp_path, "_portlin_started=$EPOCHREALTIME; (exit 130); _portlin_precmd; "
                                "print -r -- $_portlin_status")
        assert out == "%F{3}INT%f\n"

    def test_an_empty_line_clears_the_last_status(self, tmp_path):
        out = run_zsh(tmp_path, "false; _portlin_precmd; print -r -- \"[$_portlin_status]\"")
        assert out == "[]\n"

    def test_outside_a_repository_there_is_no_git_segment(self, tmp_path):
        assert run_zsh(tmp_path, "_portlin_git_status; print -r -- \"[$_portlin_git]\"") == "[]\n"


@needs_zsh
@needs_git
class TestGitSegment:
    @pytest.fixture
    def repo(self, tmp_path):
        remote, repo = tmp_path / "remote.git", tmp_path / "repo"
        remote.mkdir()
        repo.mkdir()
        git(remote, "init", "-q", "--bare", "-b", "main")
        git(repo, "init", "-q", "-b", "main")
        (repo / "a").write_text("a\n")
        git(repo, "add", "a")
        git(repo, "commit", "-qm", "one")
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "-q", "-u", "origin", "main")
        return repo

    def segment(self, tmp_path, repo) -> str:
        return run_zsh(tmp_path, "_portlin_git_status; print -r -- $_portlin_git", cwd=repo).strip()

    def test_a_clean_branch_is_its_name_alone(self, tmp_path, repo):
        assert self.segment(tmp_path, repo) == "%F{7}(%f%F{5}main%f%F{7})%f"

    def test_it_counts_each_kind_of_change(self, tmp_path, repo):
        (repo / "b").write_text("b\n")
        git(repo, "add", "b")
        git(repo, "commit", "-qm", "two")
        (repo / "a").write_text("changed\n")
        (repo / "c").write_text("staged\n")
        git(repo, "add", "c")
        (repo / "d").write_text("untracked\n")
        segment = self.segment(tmp_path, repo)
        assert "%F{7}↑1%f" in segment
        assert "%F{2}+1%f" in segment
        assert "%F{3}*1%f" in segment
        assert "%F{7}?1%f" in segment
        assert "↓" not in segment

    def test_a_detached_head_shows_the_commit(self, tmp_path, repo):
        git(repo, "checkout", "-q", "--detach")
        head = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout.strip()
        assert f"%F{{5}}{head}%f" in self.segment(tmp_path, repo)

    def test_a_branch_name_cannot_inject_prompt_escapes_or_commands(self, tmp_path, repo):
        git(repo, "checkout", "-q", "-b", "100%F{1}$(touch${IFS}pwned)")
        assert "100%%F{1}$(touch${IFS}pwned)" in self.segment(tmp_path, repo)
        drawn = run_zsh(tmp_path, '_portlin_git_status; print -P -- "$PROMPT"', cwd=repo)
        assert "100%F{1}$(touch${IFS}pwned)" in drawn
        assert not (repo / "pwned").exists()

    def test_it_can_be_switched_off(self, tmp_path, repo):
        out = run_zsh(tmp_path, "PORTLIN_PROMPT_GIT=0; _portlin_git_status; print -r -- \"[$_portlin_git]\"",
                      cwd=repo)
        assert out == "[]\n"


class TestPortlinShell:
    @pytest.fixture
    def tool(self):
        return load_tool("portlin-shell")

    def test_only_installed_and_listed_shells_are_offered(self, tool, tmp_path, monkeypatch):
        bash, zsh = tmp_path / "bash", tmp_path / "zsh"
        bash.write_text("")
        monkeypatch.setattr(tool, "SHELLS", {"bash": str(bash), "zsh": str(zsh)})
        assert tool.available({str(bash), str(zsh)}) == ["bash"]
        zsh.write_text("")
        assert tool.available({str(bash), str(zsh)}) == ["bash", "zsh"]
        assert tool.available({str(bash)}) == ["bash"]

    def test_etc_shells_comments_are_not_shells(self, tool, tmp_path):
        shells = tmp_path / "shells"
        shells.write_text("# /etc/shells: valid login shells\n/bin/sh\n/usr/bin/zsh\n\n")
        assert tool.listed_shells(shells) == {"/bin/sh", "/usr/bin/zsh"}
        assert tool.listed_shells(tmp_path / "missing") == set()

    def test_the_themed_rc_is_copied_into_a_home_without_one(self, tool, tmp_path):
        skel, home = tmp_path / "skel", tmp_path / "home"
        skel.mkdir()
        home.mkdir()
        (skel / ".zshrc").write_text("themed\n")
        assert tool.install_rc("zsh", home, skel) is True
        assert (home / ".zshrc").read_text() == "themed\n"

    def test_an_existing_rc_is_never_replaced(self, tool, tmp_path):
        skel, home = tmp_path / "skel", tmp_path / "home"
        skel.mkdir()
        home.mkdir()
        (skel / ".zshrc").write_text("themed\n")
        (home / ".zshrc").write_text("mine\n")
        assert tool.install_rc("zsh", home, skel) is False
        assert (home / ".zshrc").read_text() == "mine\n"
        (home / ".zshrc").unlink()
        (home / ".zshrc").symlink_to(tmp_path / "dotfiles-not-checked-out")
        assert tool.install_rc("zsh", home, skel) is False

    def test_chsh_changes_only_the_invoking_account(self, tool):
        assert tool.chsh_argv("/usr/bin/zsh", "sam", pkexec=False) == ["/usr/bin/chsh", "-s", "/usr/bin/zsh"]
        assert tool.chsh_argv("/usr/bin/zsh", "sam", pkexec=True) == [
            "pkexec", "/usr/bin/chsh", "-s", "/usr/bin/zsh", "sam"]

    def test_switching_to_an_uninstalled_shell_says_how_to_install_it(self, tool, monkeypatch, capsys):
        monkeypatch.setattr(tool, "available", lambda: ["bash"])
        assert tool.main(["zsh"]) == 1
        assert tool.INSTALL_HINT in capsys.readouterr().err

    def test_the_current_shell_is_not_changed_again(self, tool, monkeypatch, tmp_path):
        account = type("A", (), {"pw_name": "sam", "pw_dir": str(tmp_path), "pw_shell": "/bin/zsh"})
        monkeypatch.setattr(tool, "available", lambda: ["bash", "zsh"])
        monkeypatch.setattr(tool.pwd, "getpwuid", lambda uid: account)
        monkeypatch.setattr(tool, "SKEL", tmp_path / "no-skel")
        monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: pytest.fail("chsh ran"))
        assert tool.main(["zsh"]) == 0

    def test_a_refused_chsh_is_reported(self, tool, monkeypatch, tmp_path, capsys):
        account = type("A", (), {"pw_name": "sam", "pw_dir": str(tmp_path), "pw_shell": "/bin/bash"})
        monkeypatch.setattr(tool, "available", lambda: ["bash", "zsh"])
        monkeypatch.setattr(tool.pwd, "getpwuid", lambda uid: account)
        monkeypatch.setattr(tool, "SKEL", tmp_path / "no-skel")
        ran = []
        monkeypatch.setattr(tool.subprocess, "run",
                            lambda argv: ran.append(argv) or subprocess.CompletedProcess(argv, 126))
        assert tool.main(["zsh", "--pkexec"]) == 1
        assert ran == [["pkexec", "/usr/bin/chsh", "-s", "/usr/bin/zsh", "sam"]]
        assert "still bash" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["fish"], ["zsh", "--json"], ["status", "--pkexec"], ["zsh", "bash"]])
    def test_anything_else_is_a_usage_error(self, tool, argv, capsys):
        assert tool.main(argv) == 2
        assert "usage:" in capsys.readouterr().err


class TestWelcomeNamesTheRunningShell:
    @pytest.fixture
    def welcome(self):
        return load_tool("portlin-welcome")

    @pytest.mark.parametrize("output, expected", [
        ("GNU bash, version 5.2.37(1)-release (x86_64-pc-linux-gnu)\n", "bash 5.2.37"),
        ("zsh 5.9 (x86_64-debian-linux-gnu)\n", "zsh 5.9"),
    ])
    def test_the_version_is_read_from_either_shell(self, welcome, monkeypatch, output, expected):
        name = expected.split()[0]
        monkeypatch.setattr(welcome, "running_shell", lambda: f"/usr/bin/{name}")
        monkeypatch.setattr(welcome.subprocess, "run",
                            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, output, ""))
        assert welcome.shell_description() == expected

    def test_a_parent_that_is_not_a_login_shell_falls_back_to_shell(self, welcome, tmp_path, monkeypatch):
        shells = tmp_path / "shells"
        shells.write_text("/bin/bash\n")
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert welcome.running_shell(ppid=os.getpid(), shells=shells) == "/bin/bash"
