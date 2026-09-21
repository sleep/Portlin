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
        assert lines[0] == "::step Copying A"
        assert "::progress 37" in lines
        assert "::progress 75" in lines
        assert lines[-1] == "::progress 100"

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

        assert tool.run_steps([step], total=0, out=io.StringIO(), execute=execute).ok
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
        assert "--separate-output" in argv and "--checklist" in argv
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

    def test_the_writer_tracks_the_percent_and_feeds_the_process(self, tool):
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
