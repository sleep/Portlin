"""portlin-migrate's own seams: how it opens a source, and how it runs a plan.

The planners are covered in test_migrate.py. What is left is the executor,
which is where the protocol is spoken, the backup renames happen, and a
failing command either stops the run or becomes a warning. All of it runs
here against a fake execute() that feeds canned rsync and tar output, on a
machine with neither.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def tool():
    return load_tool("portlin-migrate")


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


class TestOpening:
    def test_an_encrypted_root_is_opened_read_only_with_the_passphrase_on_stdin(self, tool):
        argvs = tool.open_argvs("/dev/sdb4", encrypted=True)
        assert argvs[0] == ("cryptsetup", "open", "--readonly", "--type", "luks",
                            "--key-file", "-", "/dev/sdb4", tool.MAPPING)
        assert argvs[1] == ("mount", "-o", "ro,noload", f"/dev/mapper/{tool.MAPPING}", str(tool.MOUNT))

    def test_a_plain_root_is_only_mounted(self, tool):
        assert tool.open_argvs("/dev/sdc4", encrypted=False) == [
            ("mount", "-o", "ro,noload", "/dev/sdc4", str(tool.MOUNT)),
        ]

    def test_closing_reverses_opening(self, tool):
        assert tool.close_argvs(encrypted=True) == [
            ("umount", str(tool.MOUNT)),
            ("cryptsetup", "close", tool.MAPPING),
        ]
        assert tool.close_argvs(encrypted=False) == [("umount", str(tool.MOUNT))]

    def test_open_source_retries_a_wrong_passphrase_three_times_then_gives_up(self, tool, monkeypatch, tmp_path):
        # The mount point lives under /run on a stick; here it has to be
        # somewhere this test may create.
        monkeypatch.setattr(tool, "MOUNT", tmp_path / "source")
        monkeypatch.setattr(tool, "running_disk", lambda: "/dev/sda")
        calls = []
        answers = iter(["bad", "worse", "worst"])

        class Result:
            def __init__(self, code, stdout=""):
                self.returncode, self.stdout, self.stderr = code, stdout, ""

        def run(argv, **kwargs):
            calls.append((tuple(argv), kwargs.get("input")))
            if argv[0] == "blkid":
                return Result(0, "crypto_LUKS\n")
            if argv[0] == "cryptsetup":
                return Result(tool.WRONG_PASSPHRASE)
            if argv[0] == "findmnt":
                return Result(1)
            return Result(0)

        with pytest.raises(tool.SourceError, match="passphrase"):
            tool.open_source("/dev/sdb4", passphrase=lambda: next(answers), run=run)
        # Filtered to the "open" attempts specifically: giving up also closes
        # the mapping it never managed to open, which is its own (idempotent)
        # "cryptsetup close" call.
        tries = [c for c in calls if c[0][:2] == ("cryptsetup", "open")]
        assert len(tries) == tool.PASSPHRASE_TRIES
        assert [c[1] for c in tries] == ["bad", "worse", "worst"]
        assert not any(c[0][0] == "mount" for c in calls)

    def test_open_source_gives_up_at_once_on_an_empty_retry_rather_than_spend_its_tries(
        self, tool, monkeypatch, tmp_path
    ):
        # A window feeding the passphrase on stdin has sent its one line
        # already: a second or third call to a stdin-backed passphrase()
        # reads EOF and returns "". That is a reason to stop, not a wrong
        # guess to burn a retry on.
        monkeypatch.setattr(tool, "MOUNT", tmp_path / "source")
        monkeypatch.setattr(tool, "running_disk", lambda: "/dev/sda")
        calls = []
        answers = iter(["wrong", ""])

        class Result:
            def __init__(self, code, stdout=""):
                self.returncode, self.stdout, self.stderr = code, stdout, ""

        def run(argv, **kwargs):
            calls.append((tuple(argv), kwargs.get("input")))
            if argv[0] == "blkid":
                return Result(0, "crypto_LUKS\n")
            if argv[0] == "cryptsetup":
                return Result(tool.WRONG_PASSPHRASE)
            if argv[0] == "findmnt":
                return Result(1)
            return Result(0)

        with pytest.raises(tool.SourceError, match="passphrase"):
            tool.open_source("/dev/sdb4", passphrase=lambda: next(answers), run=run)
        tries = [c for c in calls if c[0][:2] == ("cryptsetup", "open")]
        assert len(tries) == 1
        assert not any(c[0][0] == "mount" for c in calls)

    def test_the_passphrase_never_appears_in_an_argument(self, tool):
        for argv in tool.open_argvs("/dev/sdb4", encrypted=True):
            assert "hunter2" not in " ".join(argv)

    def test_the_running_drive_is_refused_by_name_not_silently(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "running_disk", lambda: "/dev/sda")
        with pytest.raises(tool.SourceError, match="running from"):
            tool.open_source("/dev/sda4", passphrase=lambda: "", run=lambda *a, **k: pytest.fail("nothing runs"))

    def test_a_mount_of_a_different_source_is_closed_before_opening_this_one(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "MOUNT", tmp_path / "source")
        monkeypatch.setattr(tool, "running_disk", lambda: "")
        calls = []

        class Result:
            def __init__(self, code, stdout=""):
                self.returncode, self.stdout, self.stderr = code, stdout, ""

        def run(argv, **kwargs):
            calls.append(tuple(argv))
            if argv[0] == "blkid":
                return Result(0, "ext4\n")
            if argv[0] == "findmnt":
                return Result(0, "/dev/sdc4\n")
            return Result(0)

        # /dev/sdc4 is mounted at MOUNT already, for a different device than
        # the one being asked for; read_release then finds nothing at MOUNT
        # (there is no real stick here), which is why this ends in SourceError
        # rather than success -- what is being checked is the ordering of the
        # calls leading up to it.
        with pytest.raises(tool.SourceError):
            tool.open_source("/dev/sdb4", passphrase=lambda: "", run=run)
        names = [c[0] for c in calls]
        assert names.index("umount") < names.index("mount")


class TestRunSteps:
    def test_progress_is_weighted_across_steps_and_the_result_is_ok(self, tool, migrate):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", weight=3000),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync", weight=1000),
        ]
        out = io.StringIO()

        def execute(step, on_line):
            on_line("sending incremental file list")
            on_line("      1,234  50%   1.00MB/s    0:00:01 (xfr#1, to-chk=1/2)")
            on_line("      2,468  100%   1.00MB/s    0:00:01 (xfr#2, to-chk=0/2)")
            return 0

        result = tool.run_steps(steps, total=4000, out=out, execute=execute)
        assert result.ok and result.warnings == ()
        lines = out.getvalue().splitlines()
        assert lines[0].startswith("::stages ")
        assert lines[1] == "::step Copying A"
        assert "::progress 37" in lines
        assert "::progress 75" in lines
        assert lines[-2:] == ["::progress 100", "::bytes 4000 4000"]

    def test_the_outline_comes_first_and_bytes_and_files_follow_the_copy(self, tool, migrate):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a1"), progress="rsync", weight=3000),
            migrate.Step("Copying A", argv=("rsync", "a2"), progress="rsync", weight=0),
            migrate.Step("", warn="skipped something"),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync", weight=1000),
        ]
        out = io.StringIO()

        def execute(step, on_line):
            on_line("      1,234  50%   1.00MB/s    0:00:01 (xfr#3, to-chk=1/2)")
            on_line("      2,468  100%   1.00MB/s    0:00:01 (xfr#5, to-chk=0/2)")
            return 0

        tool.run_steps(steps, total=4000, out=out, execute=execute)
        lines = out.getvalue().splitlines()
        assert json.loads(lines[0].partition(" ")[2]) == [
            {"text": "Copying A", "bytes": 3000},
            {"text": "Copying B", "bytes": 1000},
        ]
        assert "::bytes 1500 4000" in lines
        # Counts restart with each rsync and are added up across them.
        files = [int(line.split()[1]) for line in lines if line.startswith("::files ")]
        assert files == [3, 5, 8, 10, 13, 15]

    def test_tar_checkpoints_drive_progress_by_bytes(self, tool, migrate):
        step = migrate.Step("Restoring", argv=("tar", "x"), progress="tar", weight=4 * migrate.TAR_RECORD_BYTES * 1000)
        out = io.StringIO()

        def execute(step, on_line):
            on_line("tar: Read checkpoint 2000")
            return 0

        tool.run_steps([step], total=step.weight, out=out, execute=execute)
        assert "::progress 50" in out.getvalue().splitlines()

    def test_a_tolerated_exit_is_a_warning_and_the_run_goes_on(self, tool, migrate):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", tolerate=(23,)),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync"),
        ]
        out = io.StringIO()
        codes = iter([23, 0])
        result = tool.run_steps(steps, total=0, out=out, execute=lambda step, on_line: next(codes))
        assert result.ok
        assert len(result.warnings) == 1 and "23" in result.warnings[0]
        assert any(line.startswith("::warn") for line in out.getvalue().splitlines())
        assert "::step Copying B" in out.getvalue()

    def test_an_optional_step_may_fail_but_an_ordinary_one_stops_the_run(self, tool, migrate):
        steps = [
            migrate.Step("Telling systemd", argv=("timedatectl",), optional=True),
            migrate.Step("Creating the account", argv=("useradd",)),
            migrate.Step("Never reached", argv=("rsync",)),
        ]
        ran = []

        def execute(step, on_line):
            ran.append(step.argv[0])
            return 1

        result = tool.run_steps(steps, total=0, out=io.StringIO(), execute=execute)
        assert not result.ok
        assert "useradd exited with status 1" in result.failure
        assert ran == ["timedatectl", "useradd"]

    def test_move_aside_write_and_mkdir_happen_before_the_command(self, tool, migrate, tmp_path):
        existing = tmp_path / "home/alice/Documents/notes.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("mine")
        backup = tmp_path / "home/alice/.portlin-migrate-backup/stamp/Documents/notes.txt"
        step = migrate.Step(
            "Restoring",
            argv=("tar", "x"),
            move_aside=((str(existing), str(backup)),),
            mkdir=(str(tmp_path / "made"),),
            write=((str(tmp_path / "etc/hostname"), "office\n"),),
        )
        seen = {}

        def execute(step, on_line):
            seen["existing"] = existing.exists()
            seen["backup"] = backup.read_text()
            seen["made"] = (tmp_path / "made").is_dir()
            seen["hostname"] = (tmp_path / "etc/hostname").read_text()
            return 0

        result = tool.run_steps([step], total=0, out=io.StringIO(), execute=execute,
                                move_roots=((tmp_path / "home/alice", tmp_path / "home/alice"),))
        assert result.ok
        assert seen == {"existing": False, "backup": "mine", "made": True, "hostname": "office\n"}

    def test_a_passthrough_step_forwards_the_installers_events_but_not_its_result(self, tool, migrate):
        step = migrate.Step("Installing vlc", argv=("/usr/bin/portlin-install", "install", "vlc"),
                            passthrough=True, optional=True)
        out = io.StringIO()

        def execute(step, on_line):
            on_line("::step Installing packages")
            on_line("::progress 40")
            on_line("Get:1 http://deb.debian.org ...")
            on_line("::result failed vlc apt-get exited with status 100")
            return 1

        result = tool.run_steps([step], total=0, out=out, execute=execute)
        lines = out.getvalue().splitlines()
        assert result.ok
        assert "::step Installing packages" in lines
        assert "Get:1 http://deb.debian.org ..." in lines
        assert not any(line.startswith("::result") for line in lines)
        # The child's own ::result failed line already recorded a warning;
        # the exit code 1 that goes with it is the same failure, not a second one.
        assert len(result.warnings) == 1 and "vlc" in result.warnings[0]

    def test_a_warn_only_step_is_emitted_and_runs_nothing(self, tool, migrate):
        out = io.StringIO()
        result = tool.run_steps(
            [migrate.Step("", warn="uid 1500 is already in use")], total=0, out=out,
            execute=lambda *a: pytest.fail("nothing to execute"),
        )
        assert result.ok
        assert "::warn uid 1500 is already in use" in out.getvalue()


class TestRunStepsErrorHandling:
    """execute() itself can raise -- a missing binary is FileNotFoundError, a
    child that hung up is BrokenPipeError -- and that must land in the
    protocol the same way a bad exit code does, not as a traceback with no
    ::result line. Cancellation (KeyboardInterrupt, from main's signal
    handler) is the one exception this must never turn into a warning: it
    has to propagate so the caller's cleanup runs.
    """

    def test_a_missing_binary_on_an_optional_step_is_a_warning_and_the_run_goes_on(self, tool, migrate):
        steps = [
            migrate.Step("Telling systemd", argv=("timedatectl",), optional=True),
            migrate.Step("Copying", argv=("rsync", "a")),
        ]
        ran = []

        def execute(step, on_line):
            ran.append(step.argv[0])
            if step.argv[0] == "timedatectl":
                raise FileNotFoundError("no such file: timedatectl")
            return 0

        result = tool.run_steps(steps, total=0, out=io.StringIO(), execute=execute)
        assert result.ok
        assert len(result.warnings) == 1 and "timedatectl" in result.warnings[0]
        assert ran == ["timedatectl", "rsync"]

    def test_a_missing_binary_on_an_ordinary_step_stops_the_run(self, tool, migrate):
        step = migrate.Step("Creating the account", argv=("useradd",))

        def execute(step, on_line):
            raise FileNotFoundError("no such file: useradd")

        result = tool.run_steps([step], total=0, out=io.StringIO(), execute=execute)
        assert not result.ok
        assert "useradd" in result.failure

    def test_a_cancelled_step_is_not_swallowed(self, tool, migrate):
        step = migrate.Step("Copying", argv=("rsync", "a"))

        def execute(step, on_line):
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            tool.run_steps([step], total=0, out=io.StringIO(), execute=execute)


class TestApplySteps:
    @pytest.fixture
    def parts(self, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        source_root = tmp_path / "source"
        populate_home(make_source(source_root))
        state = source_root / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text("{}")
        inventory = migrate.build_inventory(source_root)
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        return source_root, inventory, target

    def test_the_order_is_identity_then_files_then_theme_then_software(self, tool, migrate, parts):
        source_root, inventory, target = parts
        source = tool.Source("stick", "/dev/sdb4", root=source_root)
        ids = ["identity.hostname", "home.files.Documents", "software.mullvad"]
        steps = tool.apply_steps(inventory, ids, source, target, "stamp", firstboot=False,
                                 hosts_text="127.0.0.1\tlocalhost\n", icon_theme_installed=lambda n: True)
        assert [s.argv[0] for s in steps] == ["hostnamectl", "rsync", migrate.INSTALLER]

    def test_first_boot_leaves_identity_to_the_wizard(self, tool, migrate, parts):
        source_root, inventory, target = parts
        source = tool.Source("stick", "/dev/sdb4", root=source_root)
        steps = tool.apply_steps(inventory, ["identity.hostname", "home.files.Documents"], source, target, "stamp",
                                 firstboot=True, hosts_text="", icon_theme_installed=lambda n: True)
        assert [s.argv[0] for s in steps] == ["rsync"]

    def test_an_archive_source_plans_a_tar_extract(self, tool, migrate, parts):
        from test_migrate import LISTING
        source_root, inventory, target = parts
        source = tool.Source("archive", "/media/x/a.portlin-backup.tar.zst", listing=LISTING)
        steps = tool.apply_steps(inventory, ["home.files.Documents"], source, target, "stamp",
                                 firstboot=False, hosts_text="", icon_theme_installed=lambda n: True)
        assert steps[0].argv[0] == "tar"
        assert steps[1].argv[0] == "chown"


class TestPlanFile:
    def test_round_trips_through_json(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan = tmp_path / "plan.json"
        source = tool.Source("stick", "/dev/sdb4", encrypted=True)
        tool.write_plan(plan, source=source, ids=["account.olduser"], inventory=inventory,
                        firstboot=True, label="SanDisk Ultra 57.3G")
        data = tool.read_plan(plan)
        assert data["source"] == "/dev/sdb4" and data["kind"] == "stick" and data["encrypted"] is True
        assert data["ids"] == ["account.olduser"]
        assert data["firstboot"] is True
        assert data["label"] == "SanDisk Ultra 57.3G"
        assert migrate.from_json(json.dumps(data["inventory"])) == inventory
        assert data["account"] == {"name": "olduser", "gecos": "Old User", "sudo_nopasswd": True, "autologin": False}
        assert data["identity"] == {"hostname": "office", "locale": "en_GB.UTF-8", "keyboard": "gb", "timezone": "Europe/London"}
        # The plan holds the account's password hash, so it is never world
        # or group readable, not even momentarily while it is being written.
        assert stat.S_IMODE(plan.stat().st_mode) == 0o600

    def test_a_plan_without_an_account_item_reports_no_account(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan = tmp_path / "plan.json"
        tool.write_plan(plan, source=tool.Source("stick", "/dev/sdb4"), ids=["identity.hostname"],
                        inventory=inventory, firstboot=True, label="x")
        assert tool.read_plan(plan)["account"] is None


class TestLocalTarget:
    def test_the_invoking_user_wins_over_the_first_account(self, tool, monkeypatch):
        import pwd

        class Record:
            pw_name, pw_uid, pw_gid, pw_dir = "alice", 1000, 1000, "/home/alice"

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: Record)
        target = tool.local_target({"PKEXEC_UID": "1000"})
        assert (target.user, target.uid, target.gid, target.home) == ("alice", 1000, 1000, "home/alice")

    def test_no_invoking_user_and_no_account_means_an_empty_target(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "LOCAL_ROOT", tmp_path)
        assert tool.local_target({}) == tool.Target(root=Path("/"))


class TestMainErrorHandling:
    """main() is the last line of defence: whatever a verb raises, someone is
    reading stdout for a ::result line, not a traceback on stderr."""

    def test_a_missing_plan_ends_in_a_result_line_not_a_traceback(self, tool, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        code = tool.main(["apply", "--plan", str(tmp_path / "missing.json")])
        assert code == tool.EXIT_FAILED
        assert "::result failed" in capsys.readouterr().out

    def test_a_cancelled_run_ends_in_a_result_line_not_a_traceback(self, tool, monkeypatch, capsys):
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        def cancelled(args):
            raise KeyboardInterrupt()

        monkeypatch.setattr(tool, "VERBS", {**tool.VERBS, "candidates": cancelled})
        code = tool.main(["candidates"])
        assert code == tool.EXIT_FAILED
        assert "::result failed cancelled" in capsys.readouterr().out


class TestChecklist:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        return migrate.build_inventory(tmp_path)

    def test_rows_group_items_under_unselectable_headings(self, tool, inventory):
        rows = tool.checklist_rows(inventory)
        tags = [tag for tag, _, _ in rows]
        assert tags[0] == f"{tool.HEADING_PREFIX}account"
        assert rows[0][1] == "-- Account --" and rows[0][2] is False
        assert "home.files.Documents" in tags
        docs = next(row for row in rows if row[0] == "home.files.Documents")
        assert docs[1].startswith("  Documents") and "1 KB" in docs[1] and docs[2] is True
        cache = next(row for row in rows if row[0] == "home.settings..cache")
        assert cache[2] is False
        # Headings only for categories that have something in them.
        assert f"{tool.HEADING_PREFIX}network" not in tags

    def test_a_note_is_shown_after_the_label(self, tool, migrate):
        inventory = migrate.Inventory("0.1.2", "office", "home/x", (
            migrate.Item("software.nvidia", "software", "NVIDIA driver", note="describes the old machine", default=False),
        ))
        row = tool.checklist_rows(inventory)[1]
        assert "NVIDIA driver" in row[1] and "(describes the old machine)" in row[1]

    def test_the_checklist_asks_for_one_tag_per_line(self, tool):
        argv = tool.checklist_argv("What to bring over", "Tick what you want.", [("a", "A", True), ("b", "B", False)])
        assert argv[0] == "whiptail"
        assert "--separate-output" in argv and "--checklist" in argv and "--notags" in argv
        assert argv[-6:] == ["a", "A", "on", "b", "B", "off"]

    def test_parsing_drops_headings_and_blank_lines(self, tool):
        assert tool.parse_checklist("--account\naccount.olduser\n\nhome.files.Documents\n") == [
            "account.olduser", "home.files.Documents",
        ]

    def test_the_summary_names_the_count_size_source_and_free_space(self, tool, inventory):
        text = tool.plan_summary(inventory, ["home.files.Documents", "home.files.Downloads"], "SanDisk Ultra 57.3G", free=50_000_000_000)
        assert "2 items, 6 KB from SanDisk Ultra 57.3G" in text
        assert "50 GB" in text
        assert "moved aside" in text


class TestNarrow:
    def test_only_and_skip_take_ids_or_categories(self, tool, migrate, tmp_path):
        from test_migrate import make_source, populate_home
        populate_home(make_source(tmp_path))
        inventory = migrate.build_inventory(tmp_path)
        ids = ["account.olduser", "home.files.Documents", "home.files.Downloads", "home.settings..config"]
        assert tool.narrow(inventory, ids, ["home.files"], None) == ["home.files.Documents", "home.files.Downloads"]
        assert tool.narrow(inventory, ids, None, ["home.files.Downloads", "account"]) == [
            "home.files.Documents", "home.settings..config",
        ]
        assert tool.narrow(inventory, ids, None, None) == ids


class TestVersions:
    def test_a_newer_source_is_noticed_and_nothing_else_is(self, migrate):
        assert migrate.newer_version("0.2.0", "0.1.2") is True
        assert migrate.newer_version("0.1.10", "0.1.2") is True
        assert migrate.newer_version("0.1.2", "0.1.2") is False
        assert migrate.newer_version("0.1.1", "0.1.2") is False
        assert migrate.newer_version("", "0.1.2") is False
        assert migrate.newer_version("0.1.2~local", "0.1.2") is False


class TestGauge:
    def test_protocol_lines_become_gauge_updates(self, tool):
        assert tool.gauge_feed("progress", "42", 10) == "XXX\n42\n\nXXX\n"
        assert tool.gauge_feed("step", "Copying Documents", 42) == "XXX\n42\nCopying Documents\nXXX\n"
        assert tool.gauge_feed("warn", "something", 42) is None

    def test_the_writer_tracks_the_percent_and_feeds_the_process(self, tool, monkeypatch, tmp_path):
        # A non-protocol line is logged, so this must not touch the real
        # /var/log path the tool defaults to.
        monkeypatch.setattr(tool, "LOG", tmp_path / "portlin-migrate.log")
        feed = io.StringIO()
        gauge = tool.Gauge(feed)
        gauge.write("::step Copying Documents\n")
        gauge.write("::progress 30\n")
        gauge.write("sending incremental file list\n")
        gauge.write("::step Copying Downloads\n")
        assert feed.getvalue() == (
            "XXX\n0\nCopying Documents\nXXX\n"
            "XXX\n30\n\nXXX\n"
            "XXX\n30\nCopying Downloads\nXXX\n"
        )
        assert gauge.percent == 30

    def test_the_window_only_events_stay_off_the_gauge_and_the_log(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "LOG", tmp_path / "portlin-migrate.log")
        feed = io.StringIO()
        gauge = tool.Gauge(feed)
        gauge.write('::stages [{"text": "Copying A", "bytes": 1}]\n::bytes 1 2\n::files 4\n')
        assert feed.getvalue() == ""
        assert not (tmp_path / "portlin-migrate.log").exists()

    def test_a_dead_gauge_process_does_not_fail_the_write(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "LOG", tmp_path / "portlin-migrate.log")

        class DeadFeed:
            def write(self, text):
                raise BrokenPipeError()

            def flush(self):
                pass

        gauge = tool.Gauge(DeadFeed())
        gauge.write("::step Copying Documents\n")  # must not raise
        assert gauge.percent == 0

    def test_close_tolerates_a_pipe_already_broken(self, tool):
        gauge = tool.Gauge(io.StringIO())

        class DeadStdin:
            def close(self):
                raise BrokenPipeError()

        class FakeProcess:
            stdin = DeadStdin()

            def wait(self):
                return 0

        gauge.process = FakeProcess()
        gauge.close()  # must not raise


class TestInteractivePlanCleanup:
    """keep_open must only outlive interactive_plan when a plan was actually
    written; a stale file left over from an earlier run must not fool a
    declined or empty run into leaving the source mounted."""

    @pytest.fixture
    def stubbed(self, tool, migrate, monkeypatch):
        monkeypatch.setattr(tool, "open_source", lambda path, passphrase: tool.Source("stick", "/dev/sdb4"))
        monkeypatch.setattr(tool, "source_inventory",
                            lambda source, firstboot: migrate.Inventory("0.1.2", "office", "home/x", ()))
        monkeypatch.setattr(tool, "message", lambda title, text: None)
        closed = []
        monkeypatch.setattr(tool, "close_source", lambda: closed.append(True))
        return closed

    def test_nothing_chosen_releases_a_kept_open_source_despite_a_stale_plan_file(
        self, tool, stubbed, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(tool, "checklist", lambda title, text, rows: [])
        out_path = tmp_path / "plan.json"
        out_path.write_text("stale plan from an earlier run")
        code = tool.interactive_plan("/dev/sdb4", firstboot=True, keep_open=True, out_path=out_path)
        assert code == tool.EXIT_FAILED
        assert stubbed == [True]

    def test_a_declined_confirmation_releases_a_kept_open_source_despite_a_stale_plan_file(
        self, tool, stubbed, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(tool, "checklist", lambda title, text, rows: ["account.olduser"])
        monkeypatch.setattr(tool, "confirm", lambda title, text: False)
        out_path = tmp_path / "plan.json"
        out_path.write_text("stale plan from an earlier run")
        code = tool.interactive_plan("/dev/sdb4", firstboot=True, keep_open=True, out_path=out_path)
        assert code == tool.EXIT_FAILED
        assert stubbed == [True]

    def test_a_written_plan_leaves_a_kept_open_source_mounted(self, tool, stubbed, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "checklist", lambda title, text, rows: ["account.olduser"])
        monkeypatch.setattr(tool, "confirm", lambda title, text: True)
        monkeypatch.setattr(tool, "write_plan", lambda *a, **k: None)
        out_path = tmp_path / "plan.json"
        code = tool.interactive_plan("/dev/sdb4", firstboot=True, keep_open=True, out_path=out_path)
        assert code == tool.EXIT_OK
        assert stubbed == []

    def test_without_keep_open_the_source_is_always_released(self, tool, stubbed, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "checklist", lambda title, text, rows: ["account.olduser"])
        monkeypatch.setattr(tool, "confirm", lambda title, text: True)
        monkeypatch.setattr(tool, "write_plan", lambda *a, **k: None)
        out_path = tmp_path / "plan.json"
        code = tool.interactive_plan("/dev/sdb4", firstboot=True, keep_open=False, out_path=out_path)
        assert code == tool.EXIT_OK
        assert stubbed == [True]


class TestApplyPlanPassphrase:
    def test_the_passphrase_callable_reaches_open_source(self, tool, migrate, monkeypatch, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan_path = tmp_path / "plan.json"
        tool.write_plan(plan_path, source=tool.Source("stick", "/dev/sdb4"), ids=["identity.hostname"],
                        inventory=inventory, firstboot=True, label="x")
        seen = {}

        def fake_open_source(path, passphrase):
            seen["passphrase"] = passphrase
            raise tool.SourceError("stop here, the passphrase was already recorded")

        monkeypatch.setattr(tool, "open_source", fake_open_source)
        sentinel = lambda: "hunter2"
        with pytest.raises(tool.SourceError):
            tool.apply_plan(plan_path, out=io.StringIO(), passphrase=sentinel)
        assert seen["passphrase"] is sentinel


class TestApplyGauge:
    @pytest.fixture
    def stub_gauge(self, tool, monkeypatch):
        monkeypatch.setattr(tool, "Gauge", lambda: type("FakeGauge", (), {"close": lambda self: None})())
        monkeypatch.setattr(tool, "message", lambda title, text: None)

    def test_only_and_skip_still_reach_apply_plan_under_a_gauge(self, tool, stub_gauge, monkeypatch):
        calls = {}

        def fake_apply_plan(path, *, out, only=None, skip=None, passphrase=None):
            calls["only"], calls["skip"], calls["passphrase"] = only, skip, passphrase
            return tool.EXIT_OK, "done"

        monkeypatch.setattr(tool, "apply_plan", fake_apply_plan)

        class Args:
            plan = "plan.json"
            gauge = True
            only = ["home.files"]
            skip = None

        code = tool.cmd_apply(Args())
        assert code == tool.EXIT_OK
        assert calls["only"] == ["home.files"] and calls["skip"] is None
        # The passphrase must be a dialog, never the raw-stdin reader used by
        # the plain (non-gauge) path.
        assert calls["passphrase"] is not tool.passphrase_from_stdin


class TestConsoleVerbsSourceError:
    def test_cmd_plan_shows_a_dialog_and_releases_the_source(self, tool, monkeypatch):
        def raising(*a, **k):
            raise tool.SourceError("could not open /dev/sdb4")

        monkeypatch.setattr(tool, "interactive_plan", raising)
        closed = []
        monkeypatch.setattr(tool, "close_source", lambda: closed.append(True))
        shown = []
        monkeypatch.setattr(tool, "message", lambda title, text: shown.append((title, text)))

        class Args:
            source = "/dev/sdb4"
            out = "/tmp/plan.json"
            firstboot = False
            keep_open = False

        code = tool.cmd_plan(Args())
        assert code == tool.EXIT_FAILED
        assert closed == [True]
        assert shown and "/dev/sdb4" in shown[0][1]

    def test_cmd_restore_shows_a_dialog_and_releases_the_source(self, tool, monkeypatch):
        def raising(*a, **k):
            raise tool.SourceError("could not open /dev/sdb4")

        monkeypatch.setattr(tool, "interactive_plan", raising)
        closed = []
        monkeypatch.setattr(tool, "close_source", lambda: closed.append(True))
        shown = []
        monkeypatch.setattr(tool, "message", lambda title, text: shown.append((title, text)))

        class Args:
            source = "/dev/sdb4"

        code = tool.cmd_restore(Args())
        assert code == tool.EXIT_FAILED
        assert closed == [True]
        assert shown and "/dev/sdb4" in shown[0][1]


class TestHostileSourceAtRun:
    """The executor's own line of defence, for the plan data it is handed."""

    def test_a_move_aside_outside_the_allowed_roots_is_refused(self, tool, migrate, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "passwd").write_text("root:x:0:0")
        home = tmp_path / "home/alice"
        home.mkdir(parents=True)
        (home / "Documents").symlink_to(outside)
        existing = home / "Documents/passwd"
        backup = home / ".portlin-migrate-backup/stamp/Documents/passwd"
        step = migrate.Step("Restoring", argv=("tar", "x"), move_aside=((str(existing), str(backup)),))
        result = tool.run_steps([step], total=0, out=io.StringIO(), move_roots=((home, home),),
                                execute=lambda *a: pytest.fail("the step must not run"))
        assert not result.ok
        assert "passwd" in result.failure
        assert (outside / "passwd").read_text() == "root:x:0:0"
        assert not backup.exists()

    def test_a_move_aside_with_no_roots_given_is_refused(self, tool, migrate, tmp_path):
        existing = tmp_path / "home/alice/notes.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("mine")
        step = migrate.Step("Restoring", argv=("tar", "x"), move_aside=((str(existing), str(tmp_path / "b")),))
        result = tool.run_steps([step], total=0, out=io.StringIO(),
                                execute=lambda *a: pytest.fail("the step must not run"))
        assert not result.ok and existing.read_text() == "mine"

    def test_a_move_aside_inside_a_root_goes_ahead(self, tool, migrate, tmp_path):
        existing = tmp_path / "home/alice/notes.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("mine")
        backup = tmp_path / "home/alice/.portlin-migrate-backup/stamp/notes.txt"
        step = migrate.Step("Restoring", argv=("tar", "x"), move_aside=((str(existing), str(backup)),))
        result = tool.run_steps([step], total=0, out=io.StringIO(),
                                move_roots=((tmp_path / "home/alice", tmp_path / "home/alice"),),
                                execute=lambda *a: 0)
        assert result.ok and backup.read_text() == "mine" and not existing.exists()

    def test_apply_plan_confines_moves_to_the_home_etc_var_and_the_keyrings(self, tool, migrate):
        backups = Path("/var/backups")
        system = ((Path("/etc"), backups), (Path("/var"), backups), (Path("/usr/share/keyrings"), backups))
        assert tool.move_roots(migrate.Target(Path("/"), "alice", 1000, 1000, "home/alice")) == (
            (Path("/home/alice"), Path("/home/alice")), *system,
        )
        assert tool.move_roots(migrate.Target(Path("/"))) == system

    def test_a_private_write_is_created_unreadable_to_others(self, tool, migrate, tmp_path):
        path = tmp_path / "export/manifest.json"
        step = migrate.Step("Writing", write=((str(path), "{}\n"),), private=True)
        assert tool.run_steps([step], total=0, out=io.StringIO()).ok
        assert path.read_text() == "{}\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # Rewritten in place, the mode is set again rather than inherited.
        path.chmod(0o644)
        assert tool.run_steps([step], total=0, out=io.StringIO()).ok
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_the_export_manifest_is_private_and_removed_afterwards(self, tool, migrate, monkeypatch, tmp_path):
        from test_migrate import make_source, populate_home
        source = tmp_path / "s"
        populate_home(make_source(source))
        inventory = migrate.build_inventory(source)
        monkeypatch.setattr(tool, "PLAN_DIR", tmp_path / "run")
        monkeypatch.setattr(tool, "build_inventory", lambda root, **kw: inventory)
        monkeypatch.setattr(tool, "command_output", lambda argv: "")
        monkeypatch.setattr(tool, "local_target", lambda: migrate.Target(Path("/")))
        real_run_steps = tool.run_steps
        seen = {}

        def run_steps(steps, *, total, out=None, **kwargs):
            manifest, archive = steps
            assert manifest.private
            # Only the manifest's own write happens, so its mode can be
            # checked while it exists; tar itself is not run here.
            result = real_run_steps([manifest], total=0, out=io.StringIO())
            written = Path(manifest.write[0][0])
            seen["mode"] = stat.S_IMODE(written.stat().st_mode)
            seen["dir"] = written.parent
            return result

        monkeypatch.setattr(tool, "run_steps", run_steps)
        monkeypatch.setattr(tool, "emit", lambda *a, **k: None)
        args = type("Args", (), {"file": str(tmp_path / "out.tar.zst")})()
        assert tool.cmd_export(args) == tool.EXIT_OK
        export_dir = tmp_path / "run/export"
        assert seen == {"mode": 0o600, "dir": export_dir}
        assert export_dir.is_dir() and stat.S_IMODE(export_dir.stat().st_mode) == 0o700
        assert not (export_dir / "manifest.json").exists()

    def test_the_export_manifest_is_removed_even_when_the_archive_fails(self, tool, migrate, monkeypatch, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        monkeypatch.setattr(tool, "PLAN_DIR", tmp_path / "run")
        monkeypatch.setattr(tool, "build_inventory", lambda root, **kw: inventory)
        monkeypatch.setattr(tool, "command_output", lambda argv: "")
        monkeypatch.setattr(tool, "local_target", lambda: migrate.Target(Path("/")))
        monkeypatch.setattr(tool, "emit", lambda *a, **k: None)
        real_run_steps = tool.run_steps

        def run_steps(steps, *, total, out=None, **kwargs):
            real_run_steps([steps[0]], total=0, out=io.StringIO())
            return tool.RunResult(False, (), "tar exited with status 2")

        monkeypatch.setattr(tool, "run_steps", run_steps)
        args = type("Args", (), {"file": str(tmp_path / "out.tar.zst")})()
        assert tool.cmd_export(args) == tool.EXIT_FAILED
        assert not (tmp_path / "run/export/manifest.json").exists()

    def test_software_not_in_the_catalog_is_warned_about_and_not_installed(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        source_root = tmp_path / "source"
        make_source(source_root)
        items = (
            migrate.Item("software.mullvad", "software", "Mullvad", value="mullvad"),
            migrate.Item("software.evil", "software", "Evil", value="--help"),
            migrate.Item("software.other", "software", "Other", value="not-in-the-catalog"),
        )
        inventory = migrate.Inventory(version="0.1.2", hostname="office", home="home/olduser", items=items)
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        source = tool.Source("stick", "/dev/sdb4", root=source_root)
        steps = tool.apply_steps(inventory, [i.id for i in items], source, target, "stamp", firstboot=False,
                                 hosts_text="", icon_theme_installed=lambda n: True)
        installs = [s.argv for s in steps if s.argv]
        assert installs == [(migrate.INSTALLER, "install", "mullvad")]
        warned = [s.warn for s in steps if s.warn]
        assert len(warned) == 2 and any("'--help'" in w for w in warned) and any("not-in-the-catalog" in w for w in warned)

    def test_the_account_shell_is_checked_against_the_local_shells_file(self, tool, tmp_path):
        shells = tmp_path / "shells"
        shells.write_text("# /etc/shells: valid login shells\n/bin/sh\n/bin/bash\n\n/usr/bin/zsh\n")
        assert tool.local_shells(shells) == {"/bin/sh", "/bin/bash", "/usr/bin/zsh"}
        assert tool.local_shells(tmp_path / "missing") == set()

    def test_a_move_aside_whose_backup_side_leads_outside_the_roots_is_refused(self, tool, migrate, tmp_path):
        home = tmp_path / "home/alice"
        home.mkdir(parents=True)
        (home / "notes.txt").write_text("mine")
        outside = tmp_path / "outside"
        outside.mkdir()
        (home / ".portlin-migrate-backup").symlink_to(outside)
        backup = home / ".portlin-migrate-backup/stamp/notes.txt"
        step = migrate.Step("Restoring", argv=("tar", "x"), move_aside=((str(home / "notes.txt"), str(backup)),))
        result = tool.run_steps([step], total=0, out=io.StringIO(), move_roots=((home, home),),
                                execute=lambda *a: pytest.fail("the step must not run"))
        assert not result.ok and "notes.txt" in result.failure
        assert (home / "notes.txt").read_text() == "mine"
        # Nothing was created on the far side of the link either.
        assert list(outside.iterdir()) == []

    def test_a_move_aside_must_back_up_into_the_root_the_file_came_from(self, tool, migrate, tmp_path):
        home = tmp_path / "home/alice"
        home.mkdir(parents=True)
        (home / "notes.txt").write_text("mine")
        var = tmp_path / "var"
        var.mkdir()
        step = migrate.Step("Restoring", argv=("tar", "x"),
                            move_aside=((str(home / "notes.txt"), str(var / "backups/stamp/notes.txt")),))
        roots = ((home, home), (tmp_path / "etc", var / "backups"), (var, var / "backups"))
        result = tool.run_steps([step], total=0, out=io.StringIO(), move_roots=roots,
                                execute=lambda *a: pytest.fail("the step must not run"))
        assert not result.ok and "notes.txt" in result.failure
        assert (home / "notes.txt").read_text() == "mine" and not (var / "backups").exists()
        # A system file goes to the system backup tree, as the planner puts it.
        etc = tmp_path / "etc/cups"
        etc.mkdir(parents=True)
        (etc / "printers.conf").write_text("old")
        backup = var / "backups/portlin-migrate/stamp/etc/cups/printers.conf"
        step = migrate.Step("Restoring", argv=("tar", "x"), move_aside=((str(etc / "printers.conf"), str(backup)),))
        result = tool.run_steps([step], total=0, out=io.StringIO(), move_roots=roots, execute=lambda *a: 0)
        assert result.ok and backup.read_text() == "old"


class TestPause:
    """Pause + Unmount: "pause" on stdin stops the copy under way, the source
    is closed on the way out, and the same plan run again carries on."""

    @pytest.fixture
    def control(self, tool, monkeypatch):
        fresh = tool.Control()
        monkeypatch.setattr(tool, "CONTROL", fresh)
        return fresh

    def test_a_pause_stops_the_run_before_the_next_step(self, tool, migrate, control):
        steps = [
            migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", weight=10),
            migrate.Step("Copying B", argv=("rsync", "b"), progress="rsync", weight=10),
        ]
        ran = []

        def execute(step, on_line):
            ran.append(step.argv[1])
            control.pause()
            return 0

        with pytest.raises(tool.Paused):
            tool.run_steps(steps, total=20, out=io.StringIO(), execute=execute, control=control)
        assert ran == ["a"]

    def test_a_pause_stops_a_copy_mid_way(self, tool, migrate, control):
        # A real child that would run for half a minute: the pause has to
        # terminate it, not wait it out.
        step = migrate.Step("Copying A", progress="rsync", argv=(
            sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"))
        with pytest.raises(tool.Paused):
            tool._execute(step, lambda line: control.pause())
        assert control.proc is None

    def test_an_install_is_left_to_finish_and_the_pause_comes_after(self, tool, migrate, control):
        # Killing apt mid-install is how a system is broken; only copies stop.
        step = migrate.Step("Installing x", passthrough=True, argv=(
            sys.executable, "-c", "import time; print('a', flush=True); time.sleep(0.2); print('b')"))
        lines = []

        def on_line(line):
            lines.append(line)
            control.pause()

        assert tool._execute(step, on_line) == 0
        assert lines == ["a", "b"]
        assert control.requested

    def test_stdin_is_read_for_pause_and_anything_else_ignored(self, tool, control):
        thread = tool.listen_for_pause(io.StringIO("hunter2\nresume\npause\n"), control=control)
        thread.join(timeout=5)
        assert control.requested

    def test_the_end_of_stdin_is_not_a_pause(self, tool, control):
        # A window that crashed can no longer show the copy, but stopping it
        # would only lose work.
        tool.listen_for_pause(io.StringIO(""), control=control).join(timeout=5)
        assert not control.requested

    def test_the_passphrase_is_read_once_because_stdin_stays_open(self, tool, monkeypatch):
        lines = iter(["secret\n", "pause\n"])
        monkeypatch.setattr(tool.sys, "stdin", type("In", (), {"readline": lambda self: next(lines)})())
        read = tool.passphrase_once()
        assert read() == "secret"
        assert read() == ""

    def test_a_paused_apply_ends_in_a_paused_result(self, tool, monkeypatch, capsys):
        def paused(*args, **kwargs):
            raise tool.Paused()

        monkeypatch.setattr(tool, "apply_plan", paused)

        class Args:
            plan, gauge, only, skip = "plan.json", False, None, None

        assert tool.cmd_apply(Args()) == tool.EXIT_PAUSED
        assert "::result paused " in capsys.readouterr().out

    def test_the_source_is_closed_on_a_pause(self, tool, migrate, monkeypatch, tmp_path):
        from test_migrate import make_source
        inventory = migrate.build_inventory(make_source(tmp_path / "s").parent.parent)
        plan_path = tmp_path / "plan.json"
        tool.write_plan(plan_path, source=tool.Source("stick", "/dev/sdb4"), ids=["identity.hostname"],
                        inventory=inventory, firstboot=True, label="x")
        closed = []
        monkeypatch.setattr(tool, "open_source", lambda path, passphrase: tool.Source("stick", path, root=tmp_path))
        monkeypatch.setattr(tool, "close_source", lambda *a: closed.append(True))
        monkeypatch.setattr(tool, "shortfall", lambda needed, free: 0)

        def paused(*args, **kwargs):
            raise tool.Paused()

        monkeypatch.setattr(tool, "run_steps", paused)
        with pytest.raises(tool.Paused):
            tool.apply_plan(plan_path, out=io.StringIO())
        assert closed == [True]


class TestResumeFindsTheDrive:
    def test_the_uuid_wins_over_a_device_name_that_moved(self, tool, tmp_path):
        (tmp_path / "sdc4").touch()
        by_uuid = tmp_path / "by-uuid"
        by_uuid.mkdir()
        (by_uuid / "5d1e7c2a-93b4-4f0e-8a61-0c7d2b9e4f13").symlink_to(tmp_path / "sdc4")
        plan = {"source": "/dev/sdb4", "kind": "stick", "uuid": "5d1e7c2a-93b4-4f0e-8a61-0c7d2b9e4f13"}
        assert tool.plan_source(plan, by_uuid) == os.path.realpath(tmp_path / "sdc4")

    def test_an_absent_or_malformed_uuid_keeps_the_device_name(self, tool, tmp_path):
        assert tool.plan_source({"source": "/dev/sdb4", "kind": "stick", "uuid": "abcd1234-0000"}, tmp_path) == "/dev/sdb4"
        # The plan file is the user's; a uuid that climbs is not a uuid.
        assert tool.plan_source({"source": "/dev/sdb4", "kind": "stick", "uuid": "../../etc"}, tmp_path) == "/dev/sdb4"
        assert tool.plan_source({"source": "/dev/sdb4", "kind": "stick"}, tmp_path) == "/dev/sdb4"


class TestByteProgress:
    def test_bytes_move_between_percent_steps(self, tool, migrate):
        steps = [migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", weight=100_000)]
        out = io.StringIO()

        def execute(step, on_line):
            on_line("     12,345   0%   1.00MB/s    0:01:00 (xfr#1, to-chk=9/10)")
            on_line("     45,678   0%   1.00MB/s    0:00:40 (xfr#1, to-chk=9/10)")
            return 0

        tool.run_steps(steps, total=100_000, out=out, execute=execute, control=tool.Control())
        lines = out.getvalue().splitlines()
        assert "::bytes 12345 100000" in lines
        assert "::bytes 45678 100000" in lines
        assert "::progress 45" in lines

    def test_file_names_reach_the_details(self, tool, migrate):
        steps = [migrate.Step("Copying A", argv=("rsync", "a"), progress="rsync", weight=10)]
        out = io.StringIO()

        def execute(step, on_line):
            on_line("Documents/report.odt")
            return 0

        tool.run_steps(steps, total=10, out=out, execute=execute, control=tool.Control())
        assert "Documents/report.odt" in out.getvalue().splitlines()


POLICY = """\
htop:
  Installed: (none)
  Candidate: 3.4.1-5
  Version table:
     3.4.1-5 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
mail-transport-agent:
  Installed: (none)
  Candidate: (none)
  Version table:
cowsay:
  Installed: (none)
  Candidate: 3.03+dfsg2-8
"""


class TestInstallPackages:
    def test_only_names_with_a_candidate_are_installable(self, tool):
        assert tool.parse_policy_candidates(POLICY) == {"htop", "cowsay"}

    def run_verb(self, tool, names, *, update=0, install=0):
        calls = []

        class Done:
            def __init__(self, code, stdout=""):
                self.returncode, self.stdout = code, stdout

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:1] == ["apt-cache"]:
                return Done(0, POLICY)
            return Done(update if "update" in argv else install)

        args = type("Args", (), {"names": names})()
        return tool.cmd_install_packages(args, run=run), calls

    def test_missing_names_are_warned_about_and_the_rest_installed(self, tool, capsys):
        code, calls = self.run_verb(tool, ["htop", "google-chrome-stable", "cowsay"])
        out = capsys.readouterr().out
        assert code == tool.EXIT_OK
        install = next(argv for argv in calls if "install" in argv)
        assert install[-2:] == ["htop", "cowsay"]
        assert "-y" in install and "DPkg::Lock::Timeout=300" in install
        assert "::warn not in this stick's package sources, so not installed: google-chrome-stable" in out
        assert "::result ok installed 2 packages" in out

    def test_no_network_fails_before_asking_apt_for_anything(self, tool, capsys):
        code, calls = self.run_verb(tool, ["htop"], update=100)
        assert code == tool.EXIT_FAILED
        assert not any("install" in argv for argv in calls)
        assert "::result failed the package lists could not be refreshed" in capsys.readouterr().out

    def test_a_name_that_could_be_an_option_never_reaches_apt(self, tool, capsys):
        code, calls = self.run_verb(tool, ["-o=APT::Get::x", "htop"])
        assert all("-o=APT::Get::x" not in argv for argv in calls)
        assert "::warn not a package name" in capsys.readouterr().out

    def test_the_verb_is_wired_up(self, tool):
        args = tool.build_parser().parse_args(["install-packages", "htop", "cowsay"])
        assert args.names == ["htop", "cowsay"] and tool.VERBS["install-packages"] is tool.cmd_install_packages


class TestOtherPackagesInThePlan:
    def test_they_install_after_the_software(self, tool, migrate, tmp_path):
        from test_migrate import make_source
        source_root = tmp_path / "source"
        make_source(source_root)
        inventory = migrate.Inventory("0.1.2", "office", "home/olduser", (
            migrate.Item("software.mullvad", "software", "Mullvad", value="mullvad"),
            migrate.Item("packages.htop", "packages", "htop", value="htop"),
        ))
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        steps = tool.apply_steps(inventory, ["software.mullvad", "packages.htop"],
                                 tool.Source("stick", "/dev/sdb4", root=source_root), target, "stamp",
                                 firstboot=False, hosts_text="", icon_theme_installed=lambda n: True)
        assert [s.argv[:2] for s in steps] == [
            (migrate.INSTALLER, "install"), (migrate.PACKAGE_INSTALLER, "install-packages"),
        ]

    def test_the_image_list_is_shipped_where_the_tool_reads_it(self, migrate):
        from portlin import package

        assert f"/{package.IMAGE_PACKAGES}" == str(migrate.IMAGE_PACKAGES)
        shipped = package.text_files("portlin-runtime")[package.IMAGE_PACKAGES].split()
        # Both desktops, so neither comes back when setup removed one.
        assert "xfce4" in shipped and "labwc" in shipped
        assert all(migrate.PACKAGE_RE.fullmatch(name) for name in shipped)

    def test_the_target_drops_what_it_has_and_what_its_image_ships(self, tool, migrate, monkeypatch, tmp_path):
        (tmp_path / "var/lib/dpkg").mkdir(parents=True)
        (tmp_path / "var/lib/dpkg/status").write_text(
            "Package: vim\nStatus: install ok installed\nArchitecture: amd64\n")
        image = tmp_path / "image-packages"
        image.write_text("xfce4\nlabwc\n")
        monkeypatch.setattr(tool, "LOCAL_ROOT", tmp_path)
        monkeypatch.setattr(migrate, "IMAGE_PACKAGES", image)
        monkeypatch.setattr(tool, "read_image_packages", lambda: migrate.read_image_packages(image))
        inventory = migrate.Inventory("0.1.2", "office", "home/olduser", tuple(
            migrate.Item(f"packages.{n}", "packages", n, value=n) for n in ("vim", "xfce4", "htop")))
        kept = tool.without_local_packages(inventory)
        assert [item.value for item in kept.items] == ["htop"]


class TestPackageSourcesAtApply:
    def test_a_keyring_this_stick_has_is_never_copied_over(self, tool, migrate, tmp_path):
        source_root, target_root = tmp_path / "old", tmp_path / "new"
        for root, key in ((source_root, b"attacker"), (target_root, b"debian")):
            (root / "usr/share/keyrings").mkdir(parents=True)
            (root / "usr/share/keyrings/debian-archive-keyring.gpg").write_bytes(key)
        (source_root / "etc/apt/sources.list.d").mkdir(parents=True)
        (source_root / "etc/apt/sources.list.d/evil.list").write_text("deb https://evil.example.com/ x main\n")
        inventory = migrate.Inventory("0.1.2", "h", "home/u", (migrate.Item(
            "repos.evil.list", "repos", "evil", paths=(
                "etc/apt/sources.list.d/evil.list", "usr/share/keyrings/debian-archive-keyring.gpg")),))
        target = migrate.Target(root=target_root, user="alice", uid=1000, gid=1000, home="home/alice")
        steps = tool.apply_steps(inventory, ["repos.evil.list"], tool.Source("stick", "/dev/sdb4", root=source_root),
                                 target, "stamp", firstboot=False, hosts_text="", icon_theme_installed=lambda n: True)
        assert not any(step.argv for step in steps)
        assert any(step.warn and "debian-archive-keyring.gpg" in step.warn for step in steps)
