from __future__ import annotations

import argparse

import pytest

from portlin import cli
from portlin.errors import AbortedError, PortlinError, TargetError
from portlin.layout import GIB


class TestParseSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("32G", 32 * GIB),
            ("32GiB", 32 * GIB),
            ("32 GB", 32 * GIB),
            ("512M", 512 * 1024**2),
            ("1T", 1024**4),
            ("1.5G", int(1.5 * GIB)),
            ("2048", 2048),
        ],
    )
    def test_accepts_common_spellings(self, text, expected):
        assert cli.parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "big", "32X", "-4G"])
    def test_rejects_nonsense(self, text):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.parse_size(text)


class TestParser:
    def test_write_requires_a_target_and_a_rootfs(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["write"])

    def test_a_command_is_mandatory(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_create_accepts_build_and_write_options_together(self):
        args = cli.build_parser().parse_args(
            ["create", "-t", "/dev/sdz", "--encrypt", "--suite", "bookworm"]
        )
        assert args.target == "/dev/sdz"
        assert args.encrypt is True
        assert args.suite == "bookworm"

    def test_there_is_no_passphrase_flag(self):
        # A passphrase on the command line is visible in the process list to
        # every user on the machine and lands in shell history, so portlin
        # deliberately offers no way to pass one.
        help_text = cli.build_parser().format_help()
        for subparser_action in cli.build_parser()._subparsers._group_actions:
            for name, parser in subparser_action.choices.items():
                help_text += parser.format_help()
        assert "--passphrase" not in help_text


class TestBuildConfigFromArgs:
    def _args(self, **overrides) -> argparse.Namespace:
        values = dict(
            minimal=False,
            groups=None,
            suite="trixie",
            mirror="http://deb.debian.org/debian",
            security_mirror="http://security.debian.org/debian-security",
            extra=[],
            exclude=[],
            work_dir=None,
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_minimal_selects_the_headless_groups(self, tmp_path):
        cfg = cli._build_config(self._args(minimal=True), tmp_path / "r.tar")
        assert "desktop" not in (cfg.groups or [])

    def test_explicit_groups_are_split_on_commas(self, tmp_path):
        cfg = cli._build_config(self._args(groups="boot, system"), tmp_path / "r.tar")
        assert cfg.groups == ["boot", "system"]

    def test_minimal_and_groups_together_is_rejected(self, tmp_path):
        with pytest.raises(PortlinError, match="cannot be combined"):
            cli._build_config(self._args(minimal=True, groups="boot"), tmp_path / "r.tar")

    def test_a_supplied_work_dir_is_kept(self, tmp_path):
        cfg = cli._build_config(self._args(work_dir=tmp_path), tmp_path / "r.tar")
        assert cfg.keep_work_dir is True


class TestConfirmTarget:
    def _args(self, target: str, **overrides) -> argparse.Namespace:
        values = dict(target=target, force=False, yes=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_a_dry_run_never_prompts(self, runner):
        cli._confirm_target(self._args("/dev/sdz"), runner)

    def test_refuses_an_unsafe_device_even_with_yes(self, runner, monkeypatch):
        # --yes skips the confirmation, not the safety rules.
        monkeypatch.setattr(
            "portlin.devices.safety_problems", lambda device, force=False: ["nope"]
        )
        with pytest.raises(TargetError, match="refusing to write"):
            cli._confirm_target(self._args("/dev/sdz", yes=True), runner)

    def test_refuses_to_proceed_unattended_without_yes(self, monkeypatch):
        from portlin.runner import Runner

        runner = Runner(dry_run=False)
        monkeypatch.setattr("portlin.devices.find_device", lambda r, p: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(AbortedError, match="--yes"):
            cli._confirm_target(self._args("stick.img"), runner)

    def test_requires_the_exact_path_to_be_typed(self, monkeypatch, capsys):
        from portlin.runner import Runner

        runner = Runner(dry_run=False)
        monkeypatch.setattr("portlin.devices.find_device", lambda r, p: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        with pytest.raises(AbortedError, match="nothing was written"):
            cli._confirm_target(self._args("stick.img"), runner)

    def test_typing_the_path_proceeds(self, monkeypatch):
        from portlin.runner import Runner

        runner = Runner(dry_run=False)
        monkeypatch.setattr("portlin.devices.find_device", lambda r, p: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "stick.img")
        cli._confirm_target(self._args("stick.img"), runner)


class TestMain:
    def test_devices_lists_the_dry_run_device(self, capsys):
        assert cli.main(["--dry-run", "devices"]) == 0
        assert "/dev/sdz" in capsys.readouterr().out

    def test_errors_are_reported_without_a_traceback(self, capsys, monkeypatch):
        monkeypatch.setattr(
            "portlin.devices.list_devices",
            lambda runner: (_ for _ in ()).throw(TargetError("bad news")),
        )
        assert cli.main(["--dry-run", "devices"]) == 1
        assert "error: bad news" in capsys.readouterr().err

    def test_dry_run_prints_the_command_plan(self, capsys):
        cli.main(["--dry-run", "devices"])
        out = capsys.readouterr().out
        assert "dry run: commands that would have run" in out
        assert "lsblk" in out

    def test_package_subcommand_builds_every_package(self, tmp_path, capsys):
        cli.main(["--dry-run", "package", "--output", str(tmp_path)])
        out = capsys.readouterr().out
        assert out.count("dpkg-deb --build") == 3
