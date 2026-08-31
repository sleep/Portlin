"""The entry point's container hand-off.

This is deliberately narrow: the display needs a terminal and the build needs
root, so neither is tested here. What is tested is the wiring that decides where
the build runs and how it is re-launched, because a mount or a path that is
wrong there fails a minute into a container with an error that says nothing
about the cause.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from portlin import progress

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("portlin_build", REPO / "scripts" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def make_container_command(script, monkeypatch, tmp_path):
    """A factory for the docker invocation, without running it."""
    captured = {}

    def fake_run(command, *args, **kwargs):
        captured["command"] = command

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    monkeypatch.setattr(script.shutil, "which", lambda _name: "/usr/bin/docker")
    # Probed by run_in_container to decide whether to warn about a pull. Stubbed
    # rather than left to fake_run, so the captured command is always the
    # docker run and never the probe.
    monkeypatch.setattr(script, "_image_is_cached", lambda _image: True)

    def make(**overrides):
        fields = {
            "out_dir": tmp_path / "out", "name": "portlin", "suite": "trixie",
            "image_size": "8G", "minimal": False, "keep": False, "verbose": False,
        }
        args = script.main.__globals__["argparse"].Namespace(**{**fields, **overrides})
        script.run_in_container(args)
        return captured["command"]

    return make


@pytest.fixture
def container_command(make_container_command):
    return make_container_command()


class TestContainerHandoff:
    def test_mounts_the_repo_by_absolute_path(self, container_command):
        # Not $PWD, and not a relative path. The repo root is derived from the
        # script's own location precisely so that the working directory of
        # whoever launched it cannot decide whether the build works.
        assert "-v" in container_command
        assert f"{REPO}:/src" in container_command

    def test_runs_the_script_by_absolute_path_inside(self, container_command):
        inner = container_command[-1]
        assert "/src/scripts/build.py" in inner
        assert "--in-container" in inner

    def test_asks_for_amd64_regardless_of_the_host(self, container_command):
        # debootstrap runs amd64 maintainer scripts, so an arm64 host has to
        # emulate rather than build natively.
        assert "linux/amd64" in container_command

    def test_is_privileged_because_the_write_stage_needs_loop_devices(self, container_command):
        assert "--privileged" in container_command

    def test_output_goes_to_the_bind_mounted_directory(self, container_command, tmp_path):
        assert f"{(tmp_path / 'out').resolve()}:/out" in container_command
        assert "--out-dir /out" in container_command[-1]

    def test_the_bootstrap_installs_only_what_the_display_needs_to_exist(
        self, container_command, script
    ):
        # Everything installed out here is installed with no display to draw it,
        # so its output lands above the banner. Only python3 has to be, because
        # it is what runs the display; the rest is a stage inside the build.
        inner = container_command[-1]
        assert f"--no-install-recommends {script.CONTAINER_BOOTSTRAP} " in inner
        for tool in ("debootstrap", "zstd", "cryptsetup-bin"):
            assert tool not in inner

    def test_the_bootstrap_is_quiet_but_still_reports_failure(self, container_command):
        # >/dev/null on stdout only. dpkg's chatter is what pushes the display
        # off the screen; apt's errors are on stderr and must survive.
        inner = container_command[-1]
        assert "apt-get install -y -qq --no-install-recommends python3 >/dev/null" in inner
        assert "2>" not in inner


class TestBootstrapNarration:
    """The one stage of a build that cannot draw itself.

    python3 is what runs the display, so until it is installed there is nothing
    to draw with. On an emulated arm64 host that install is also among the
    slowest minutes of the whole run, and silence there is indistinguishable
    from a hang.
    """

    def test_each_bootstrap_step_announces_itself(self, container_command):
        inner = container_command[-1]
        for milestone in ("apt-get update", "installing python3", "handing over"):
            assert f'step "{milestone}' in inner, milestone

    def test_verbose_lets_apt_speak_without_losing_the_milestones(
        self, make_container_command
    ):
        inner = make_container_command(verbose=True)[-1]
        assert ">/dev/null" not in inner
        assert "-qq" not in inner
        assert 'step "installing python3"' in inner

    def test_the_milestones_read_like_the_rest_of_the_build(self, script):
        # The display already prints "[12s] packages" for every stage it draws.
        # A bootstrap that invented its own time format would make the first
        # three lines of a build look like they came from another program.
        helper = script._step_helper()
        for elapsed, expected in ((9, "9s"), (75, "1m15s"), (3700, "1h01m")):
            result = subprocess.run(
                ["bash", "-c", f'{helper}\nSECONDS={elapsed}\nstep "probe"'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, result.stderr
            assert f"[{expected}]" in result.stdout, result.stdout
            assert progress.format_duration(elapsed) == expected

    def test_the_rendered_bootstrap_is_valid_shell(self, container_command):
        # Rendered by string assembly and never executed on this side, so a
        # quoting mistake here would surface only inside a container, minutes
        # in, as a syntax error with no obvious author.
        inner = container_command[-1]
        result = subprocess.run(["bash", "-n", "-c", inner], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestPullAnnouncement:
    """A cold pull is the longest wait in a build and the least explained.

    docker reports its own pull progress on stderr, but nothing says beforehand
    that a pull is what the wait is, so a first run looks like a stall.
    """

    def test_a_missing_image_says_it_is_being_pulled(
        self, script, monkeypatch, make_container_command, capsys
    ):
        monkeypatch.setattr(script, "_image_is_cached", lambda _image: False)
        make_container_command()
        assert "pulling" in capsys.readouterr().out

    def test_a_cached_image_says_so_rather_than_nothing(
        self, make_container_command, capsys
    ):
        make_container_command()
        out = capsys.readouterr().out
        assert "cached" in out
        assert "pulling" not in out


class TestHostDetection:
    def test_a_mac_cannot_build_directly(self, script, monkeypatch):
        monkeypatch.setattr(script.sys, "platform", "darwin")
        assert script.can_build_here() is False

    def test_linux_without_root_cannot_build_directly(self, script, monkeypatch):
        # Every stage of the write needs loop devices, mounts and mknod.
        monkeypatch.setattr(script.sys, "platform", "linux")
        monkeypatch.setattr(script.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(script.os, "geteuid", lambda: 1000)
        assert script.can_build_here() is False

    def test_arm64_linux_cannot_build_directly(self, script, monkeypatch):
        monkeypatch.setattr(script.sys, "platform", "linux")
        monkeypatch.setattr(script.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(script.os, "geteuid", lambda: 0)
        assert script.can_build_here() is False

    def test_no_docker_fails_rather_than_silently_doing_nothing(self, script, monkeypatch, capsys):
        monkeypatch.setattr(script, "can_build_here", lambda: False)
        assert script.main(["--no-docker"]) == 2
        assert "x86_64 Linux and root" in capsys.readouterr().err

    def test_in_container_never_recurses_into_another_container(self, script, monkeypatch, capsys):
        # If the container itself cannot build, launching a nested one would
        # loop. Fail with the reason instead.
        monkeypatch.setattr(script, "can_build_here", lambda: False)
        assert script.main(["--in-container"]) == 2


class TestHeader:
    """The banner, which is also where two warnings live.

    Both are easy to lose: the header is laid out against a fixed-height logo,
    so anything past the fourth fact has nowhere obvious to go.
    """

    def _args(self, script):
        return script.main.__globals__["argparse"].Namespace(suite="trixie", image_size="8G")

    def test_warns_before_replacing_an_existing_image(self, script, tmp_path):
        # 10 GB of someone's previous build is not something to overwrite
        # without saying so.
        image = tmp_path / "portlin.img"
        image.write_bytes(b"x" * 1024)
        header = script._header(script.Theme(False), True, self._args(script), image, {})
        assert any("replacing portlin.img" in line for line in header)

    def test_says_when_the_eta_has_no_history_behind_it(self, script, tmp_path):
        header = script._header(
            script.Theme(False), True, self._args(script), tmp_path / "absent.img", {}
        )
        assert any("first run" in line for line in header)

    def test_no_warnings_on_an_ordinary_rebuild(self, script, tmp_path):
        header = script._header(
            script.Theme(False), True, self._args(script), tmp_path / "absent.img",
            {"packages": 900.0},
        )
        assert not any("first run" in line or "replacing" in line for line in header)

    def test_a_fact_is_never_dropped_when_they_all_apply(self, script, tmp_path):
        # Five facts against a six-line logo: the fifth would otherwise land on
        # the box's bottom border, and a sixth would vanish entirely.
        image = tmp_path / "portlin.img"
        image.write_bytes(b"x")
        header = script._header(script.Theme(False), True, self._args(script), image, {})
        assert any("first run" in line for line in header)
        assert any("replacing" in line for line in header)
        assert not any("replacing" in line and "└" in line for line in header)

    def test_falls_back_to_ascii_without_a_utf8_locale(self, script, tmp_path):
        header = script._header(
            script.Theme(False), False, self._args(script), tmp_path / "absent.img", {}
        )
        rendered = "\n".join(header)
        for character in ("█", "┌", "│", "·"):
            assert character not in rendered


class TestVerificationResult:
    """The last line of a build is the only one most people read.

    verify-image.sh runs with check=False, because a failed verification should
    still leave the image on disk to be inspected rather than raising through
    the display. That makes it easy to drop the result on the floor, which
    would mean announcing a bad image as ready.
    """

    @pytest.fixture
    def build_args(self, script, tmp_path):
        return script.main.__globals__["argparse"].Namespace(
            out_dir=tmp_path / "out", name="portlin", suite="trixie",
            image_size="8G", minimal=True, keep=False, in_container=False,
        )

    @pytest.fixture
    def stub_pipeline(self, script, monkeypatch):
        """Run build() without building anything, with a settable verify result."""
        outcome = {"ok": True}

        monkeypatch.setattr(script, "build_rootfs", lambda cfg, runner: cfg.output)
        monkeypatch.setattr(script, "write_stick", lambda cfg, runner: None)

        class FakeResult:
            @property
            def ok(self):
                return outcome["ok"]

        class FakeRunner:
            def __init__(self, **kwargs):
                pass

            def run(self, argv, **kwargs):
                return FakeResult()

        monkeypatch.setattr(script, "Runner", FakeRunner)
        return outcome

    def test_a_failed_verification_is_not_announced_as_ready(
        self, script, build_args, stub_pipeline, capsys
    ):
        stub_pipeline["ok"] = False
        assert script.build(build_args) == 1
        output = capsys.readouterr().out
        assert "FAILED" in output
        assert "ready" not in output

    def test_a_passing_verification_reports_success(
        self, script, build_args, stub_pipeline, capsys
    ):
        assert script.build(build_args) == 0
        assert "ready in" in capsys.readouterr().out

    def test_timings_are_kept_even_when_verification_fails(
        self, script, build_args, stub_pipeline
    ):
        # The durations were really measured; only the verdict on the image is
        # in doubt. Throwing them away would make the next ETA worse for no
        # reason.
        stub_pipeline["ok"] = False
        script.build(build_args)
        assert (build_args.out_dir / script.TIMINGS_FILE).exists()


class TestBuildTools:
    """The container's own tool install, which is a stage like any other.

    It used to run as a shell line in the docker invocation, where its two
    hundred lines of dpkg output scrolled past before the display existed and
    pushed the banner off the top of the screen.
    """

    def test_the_tools_stage_exists_only_in_a_container(self, script):
        keys = [stage.key for stage in script._stages(in_container=True)]
        assert keys[0] == "tools"
        assert "tools" not in [stage.key for stage in script._stages(in_container=False)]

    def test_the_weights_still_add_up_to_a_whole_build(self, script):
        for in_container in (True, False):
            total = sum(stage.weight for stage in script._stages(in_container))
            assert abs(total - 1.0) < 1e-9

    def test_apt_is_asked_for_a_machine_readable_percentage(self, script):
        # Without Status-Fd there is no percentage to put in the bar, which is
        # the entire reason for running this through the Runner.
        assert "APT::Status-Fd=1" in script.APT

    def test_the_install_runs_through_the_runner_pinned_to_the_stage(self, script):
        class FakeTimeline:
            def __init__(self):
                self.started = []

            def start(self, key):
                self.started.append(key)

        class FakeWatcher:
            def __init__(self):
                self.timeline = FakeTimeline()
                self.pinned = None
                self.pins_seen = []

        class FakeRunner:
            def __init__(self, watcher):
                self.watcher = watcher
                self.commands = []

            def run(self, argv, **kwargs):
                self.commands.append(argv)
                self.watcher.pins_seen.append(self.watcher.pinned)

        watcher = FakeWatcher()
        runner = FakeRunner(watcher)
        script._install_build_tools(runner, watcher)

        assert watcher.timeline.started == ["tools"]
        # Pinned for the whole install and released afterwards: unpinned, these
        # apt-get calls read as the packages stage and light up its bar early.
        assert watcher.pins_seen == ["tools", "tools"]
        assert watcher.pinned is None
        assert runner.commands[0][-1] == "update"
        assert "debootstrap" in runner.commands[1]

    def test_a_failed_tool_install_is_not_swallowed(self, script):
        class Boom(Exception):
            pass

        class FakeWatcher:
            class timeline:
                @staticmethod
                def start(key):
                    pass

            pinned = None

        class FakeRunner:
            def run(self, argv, **kwargs):
                raise Boom()

        watcher = FakeWatcher()
        with pytest.raises(Boom):
            script._install_build_tools(FakeRunner(), watcher)
        assert watcher.pinned is None
