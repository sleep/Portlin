from __future__ import annotations

import pytest

from portlin.errors import CommandError
from portlin.runner import Runner


class TestDryRun:
    def test_records_without_executing(self):
        runner = Runner(dry_run=True)
        # A command that would certainly fail if it actually ran.
        runner.run(["sgdisk", "--zap-all", "/dev/definitely-not-real"])
        assert runner.commands == [["sgdisk", "--zap-all", "/dev/definitely-not-real"]]

    def test_returns_the_supplied_placeholder_output(self):
        runner = Runner(dry_run=True)
        assert runner.output(["blkid"], dry_stdout="uuid-here") == "uuid-here"

    def test_write_file_is_recorded_but_not_performed(self, tmp_path):
        runner = Runner(dry_run=True)
        target = tmp_path / "nested" / "fstab"
        runner.write_file(target, "content")
        assert not target.exists()
        assert ["write-file", str(target), "mode=644"] in runner.commands

    def test_coerces_arguments_to_strings(self, tmp_path):
        runner = Runner(dry_run=True)
        runner.run(["mkfs.ext4", 42, tmp_path])
        assert runner.commands[0] == ["mkfs.ext4", "42", str(tmp_path)]


class TestExecution:
    def test_captures_stdout(self):
        runner = Runner()
        assert runner.output(["echo", "hello"]) == "hello"

    def test_raises_on_failure_by_default(self):
        runner = Runner()
        with pytest.raises(CommandError) as exc:
            runner.run(["sh", "-c", "echo boom >&2; exit 3"])
        assert exc.value.returncode == 3
        assert "boom" in str(exc.value)

    def test_check_false_returns_the_failure(self):
        runner = Runner()
        result = runner.run(["sh", "-c", "exit 7"], check=False)
        assert result.returncode == 7
        assert result.ok is False

    def test_stdin_is_delivered(self):
        runner = Runner()
        assert runner.output(["cat"], input="piped") == "piped"

    def test_secret_stdin_reaches_the_command_but_not_the_log(self):
        # The whole reason this parameter exists: a LUKS passphrase must never
        # appear in an argument vector, where /proc exposes it to every user.
        runner = Runner()
        assert runner.output(["cat"], secret_input="hunter2") == "hunter2"
        assert not any("hunter2" in part for cmd in runner.commands for part in cmd)

    def test_write_file_creates_parents_and_sets_mode(self, tmp_path):
        runner = Runner()
        target = tmp_path / "a" / "b" / "policy-rc.d"
        runner.write_file(target, "exit 101\n", mode=0o755)
        assert target.read_text() == "exit 101\n"
        assert target.stat().st_mode & 0o777 == 0o755

    def test_a_missing_binary_with_check_false_does_not_raise(self):
        # check=False means "I do not care if this does not work". A tool that is
        # not installed has not worked, and crashing mid-write over an optional
        # command such as udevadm would abort a stick that was otherwise fine.
        runner = Runner()
        result = runner.run(["definitely-not-a-real-binary"], check=False)
        assert result.returncode == 127
        assert result.ok is False
        assert "command not found" in result.stderr

    def test_a_missing_binary_with_check_true_raises_a_clean_error(self):
        runner = Runner()
        with pytest.raises(CommandError) as exc:
            runner.run(["definitely-not-a-real-binary"])
        assert exc.value.returncode == 127
        assert "command not found" in str(exc.value)

    def test_failed_commands_are_still_recorded(self):
        runner = Runner()
        runner.run(["sh", "-c", "exit 1"], check=False)
        assert runner.rendered() == ["sh -c exit 1"]


class TestStreaming:
    """The progress hook. Without it the build is silent for an hour.

    The property that matters most is that attaching a hook changes nothing
    else: CommandResult must still separate stdout from stderr, because
    CommandError quotes stderr and every failure message in the tool depends on
    it being the error rather than the error mixed into a megabyte of apt
    chatter.
    """

    def test_lines_arrive_as_they_are_produced(self):
        seen = []
        runner = Runner(on_output=lambda argv, stream, line: seen.append(line))
        runner.run(["printf", "one\ntwo\nthree\n"])
        assert seen == ["one", "two", "three"]

    def test_the_hook_is_told_which_stream_a_line_came_from(self):
        # debootstrap reports progress on stderr and apt on stdout, so a hook
        # that cannot tell them apart cannot parse either reliably.
        seen = []
        runner = Runner(on_output=lambda argv, stream, line: seen.append((stream, line)))
        runner.run(["sh", "-c", "echo out; echo err >&2"])
        assert ("stdout", "out") in seen
        assert ("stderr", "err") in seen

    def test_the_hook_is_told_which_command_produced_the_line(self):
        # One hook watches the whole build, so the line alone is ambiguous:
        # "Unpacking ..." comes from both debootstrap and apt.
        seen = []
        runner = Runner(on_output=lambda argv, stream, line: seen.append(argv[0]))
        runner.run(["echo", "hi"])
        assert seen == ["echo"]

    def test_streaming_still_separates_stdout_from_stderr(self):
        runner = Runner(on_output=lambda *a: None)
        result = runner.run(["sh", "-c", "echo out; echo err >&2"])
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"

    def test_streaming_still_raises_with_the_error_text(self):
        runner = Runner(on_output=lambda *a: None)
        with pytest.raises(CommandError) as exc:
            runner.run(["sh", "-c", "echo bad thing >&2; exit 3"])
        assert "bad thing" in str(exc.value)

    def test_a_hook_that_raises_does_not_take_the_build_down(self):
        # The hook draws a progress bar. A terminal that went away, or a bug in
        # the rendering, must not destroy an hour of build work.
        def explode(argv, stream, line):
            raise RuntimeError("bad hook")

        runner = Runner(on_output=explode)
        assert runner.run(["echo", "fine"]).ok

    def test_a_dry_run_never_calls_the_hook(self):
        seen = []
        runner = Runner(dry_run=True, on_output=lambda *a: seen.append(a))
        runner.run(["echo", "hi"])
        assert seen == []

    def test_secret_stdin_is_still_never_recorded(self):
        # Streaming introduces a second execution path, so the guarantee that
        # passphrases stay out of the command log has to hold on both.
        runner = Runner(on_output=lambda *a: None)
        runner.run(["cat"], secret_input="hunter2")
        assert "hunter2" not in " ".join(" ".join(c) for c in runner.commands)
