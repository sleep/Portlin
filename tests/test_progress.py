"""The parsers are the whole reason the progress bars can be trusted.

Every percentage the TUI shows comes from one of these functions reading a line
some other program printed. A parser that silently returns None turns a real
progress bar into a stalled one, and a parser that raises takes the build down
with it, so the interesting cases here are the malformed and the surprising
input rather than the happy path.
"""

from __future__ import annotations

import json

import pytest

from portlin import progress


class TestAptStatus:
    """apt-get -o APT::Status-Fd=1 emits kind:package:percent:description."""

    def test_reads_an_unpack_line(self):
        status = progress.parse_apt_status("pmstatus:xfce4-panel:41.7:Unpacking xfce4-panel")
        assert status is not None
        assert status.package == "xfce4-panel"
        assert status.fraction == pytest.approx(0.417)
        assert status.detail == "Unpacking xfce4-panel"

    def test_reads_a_download_line(self):
        # The package field is an item number during download, not a name.
        status = progress.parse_apt_status("dlstatus:1:12.5:Retrieving file 1 of 8")
        assert status is not None
        assert status.fraction == pytest.approx(0.125)
        assert status.detail == "Retrieving file 1 of 8"

    def test_survives_a_multiarch_package_name(self):
        # libc6:amd64 puts a colon inside the package field, so splitting on
        # colons naively shifts every later field along by one and reads the
        # architecture as the percentage.
        status = progress.parse_apt_status(
            "pmstatus:libc6:amd64:63.2:Setting up libc6:amd64 (2.41-12)"
        )
        assert status is not None
        assert status.package == "libc6:amd64"
        assert status.fraction == pytest.approx(0.632)

    def test_keeps_colons_in_the_description(self):
        status = progress.parse_apt_status(
            "pmstatus:grub-pc:10.0:Preparing to unpack .../grub-pc_2.12_amd64.deb: done"
        )
        assert status is not None
        assert status.detail.endswith(": done")

    def test_clamps_a_percentage_out_of_range(self):
        # apt has been known to report slightly over 100 at the end of a run.
        status = progress.parse_apt_status("pmstatus:x:100.5:Done")
        assert status is not None
        assert status.fraction == 1.0

    def test_ignores_kinds_that_are_not_progress(self):
        # pmconffile means apt is asking about a conffile; media-change means it
        # wants a disc. Neither is a percentage and both would otherwise parse.
        assert progress.parse_apt_status("pmconffile:/etc/x:y:z") is None
        assert progress.parse_apt_status("media-change:1:2:insert disc") is None

    def test_ignores_ordinary_apt_output(self):
        for line in ("Setting up libc6:amd64 (2.41-12) ...", "", "Reading database", "::"):
            assert progress.parse_apt_status(line) is None


class TestDebootstrapStatus:
    def test_reads_the_verb_and_the_package(self):
        step = progress.parse_debootstrap("I: Retrieving libc6 2.41-12")
        assert step == ("Retrieving", "libc6")

    def test_reads_a_phase_line_with_no_package(self):
        step = progress.parse_debootstrap("I: Unpacking the base system...")
        assert step is not None
        assert step[0] == "Unpacking"

    def test_ignores_warnings_and_noise(self):
        assert progress.parse_debootstrap("W: Failure trying to run: chroot") is None
        assert progress.parse_debootstrap("some other output") is None


class TestTarCheckpoints:
    def test_reads_a_write_checkpoint(self):
        assert progress.parse_tar_checkpoint("tar: Write checkpoint 2000") == 2000

    def test_reads_a_read_checkpoint(self):
        # The unpack side reports read checkpoints rather than write ones.
        assert progress.parse_tar_checkpoint("tar: Read checkpoint 18000") == 18000

    def test_ignores_other_tar_output(self):
        assert progress.parse_tar_checkpoint("tar: Removing leading '/'") is None
        assert progress.parse_tar_checkpoint("") is None

    def test_converts_records_to_bytes(self):
        # A tar record is 512 bytes. Without this the bar is out by 512x, which
        # looks like a build that finishes instantly and then hangs.
        assert progress.checkpoint_bytes(2000) == 2000 * 512


class TestFormatting:
    def test_seconds_below_a_minute(self):
        assert progress.format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert progress.format_duration(252) == "4m12s"

    def test_hours_and_minutes(self):
        assert progress.format_duration(3900) == "1h05m"

    def test_zero_and_negative_are_not_errors(self):
        # Clock skew inside a container has produced negative elapsed times.
        assert progress.format_duration(0) == "0s"
        assert progress.format_duration(-5) == "0s"

    def test_a_bar_is_exactly_the_width_asked_for(self):
        for fraction in (0.0, 0.5, 1.0, None):
            assert len(progress.render_bar(fraction, 20)) == 20

    def test_a_full_bar_has_no_empty_cells(self):
        bar = progress.render_bar(1.0, 10)
        assert bar == progress.FILL * 10

    def test_an_unknown_fraction_renders_empty_rather_than_full(self):
        assert progress.render_bar(None, 10) == progress.EMPTY * 10

    def test_a_bar_clamps_rather_than_overflowing(self):
        assert progress.render_bar(1.5, 10) == progress.FILL * 10
        assert progress.render_bar(-1.0, 10) == progress.EMPTY * 10


class TestEta:
    def test_extrapolates_from_work_done(self):
        # Half done after a minute means about another minute.
        assert progress.estimate_remaining(0.5, elapsed=60) == 60

    def test_is_unknown_before_any_progress(self):
        # Dividing by zero progress would report an infinite ETA, which is worse
        # than admitting there is not enough information yet.
        assert progress.estimate_remaining(0.0, elapsed=60) is None
        assert progress.estimate_remaining(None, elapsed=60) is None

    def test_is_never_negative_at_the_end(self):
        assert progress.estimate_remaining(1.0, elapsed=60) == 0


class TestTimeline:
    def setup_method(self):
        self.now = 0.0
        self.timeline = progress.Timeline(clock=lambda: self.now)

    def advance(self, seconds):
        self.now += seconds

    def test_starts_with_nothing_running(self):
        assert self.timeline.current is None
        assert self.timeline.overall() == 0.0

    def test_a_finished_stage_contributes_its_whole_weight(self):
        self.timeline.start("debootstrap")
        self.advance(60)
        self.timeline.finish()
        weight = self.timeline.weight_of("debootstrap")
        assert self.timeline.overall() == weight

    def test_a_running_stage_contributes_its_share(self):
        self.timeline.start("debootstrap")
        self.timeline.update(0.5)
        assert self.timeline.overall() == self.timeline.weight_of("debootstrap") * 0.5

    def test_weights_sum_to_one(self):
        assert abs(sum(s.weight for s in progress.DEFAULT_STAGES) - 1.0) < 1e-9

    def test_records_how_long_each_stage_took(self):
        self.timeline.start("debootstrap")
        self.advance(252)
        self.timeline.finish()
        assert self.timeline.durations()["debootstrap"] == 252

    def test_starting_a_stage_finishes_the_previous_one(self):
        # Nothing in the build ever runs two stages at once, and a stage left
        # running would sit at its last percentage for the rest of the build.
        self.timeline.start("debootstrap")
        self.advance(10)
        self.timeline.start("packages")
        assert "debootstrap" in self.timeline.durations()
        assert self.timeline.current == "packages"

    def test_measured_timings_replace_the_default_weights(self):
        # After one real build the weights should describe this machine, where
        # emulation makes the package install dominate far more than the
        # defaults assume.
        timeline = progress.Timeline(
            timings={"debootstrap": 100.0, "packages": 900.0},
            clock=lambda: self.now,
        )
        assert timeline.weight_of("packages") > timeline.weight_of("debootstrap")
        assert abs(sum(s.weight for s in timeline.stages) - 1.0) < 1e-9

    def test_an_unknown_stage_key_is_refused(self):
        # A typo in a stage name would silently contribute nothing to the bar.
        with pytest.raises(KeyError):
            self.timeline.start("nonsense")


class TestTimingsCache:
    def test_round_trips(self, tmp_path):
        path = tmp_path / "timings.json"
        progress.save_timings(path, {"debootstrap": 252.0, "packages": 1800.0})
        assert progress.load_timings(path)["packages"] == 1800.0

    def test_a_missing_cache_is_not_an_error(self, tmp_path):
        assert progress.load_timings(tmp_path / "absent.json") == {}

    def test_a_corrupt_cache_is_not_an_error(self, tmp_path):
        # This file exists only to make the ETA better. Taking a build down over
        # it would be an absurd trade.
        path = tmp_path / "timings.json"
        path.write_text("{not json")
        assert progress.load_timings(path) == {}

    def test_junk_values_are_dropped_rather_than_trusted(self, tmp_path):
        path = tmp_path / "timings.json"
        path.write_text(json.dumps({"packages": "soon", "tarball": -3, "unpack": 12.0}))
        assert progress.load_timings(path) == {"unpack": 12.0}

    def test_an_unwritable_cache_is_not_an_error(self, tmp_path):
        # Nothing about a successful build should fail at the last moment
        # because a cache directory is read-only.
        progress.save_timings(tmp_path / "no" / "such" / "dir" / "t.json", {"a": 1.0})


class TestStageDetection:
    """Which stage is running is derived from the command, not announced.

    The alternative is threading a stage label through build_rootfs and
    write_stick, which would put presentation concerns into the orchestration.
    The command line already says what is happening.
    """

    def test_debootstrap(self):
        assert progress.stage_for(["debootstrap", "--arch=amd64", "trixie", "/x"]) == "debootstrap"

    def test_apt_inside_the_chroot(self):
        # Every apt run reaches the runner wrapped in chroot and eatmydata, so
        # matching on argv[0] alone would never fire.
        argv = ["chroot", "/tmp/build/root", "eatmydata", "apt-get", "-y", "install", "xfce4"]
        assert progress.stage_for(argv) == "packages"

    def test_packing_and_unpacking_are_different_stages(self):
        assert progress.stage_for(["tar", "--acls", "-cf", "/out/r.tar.zst"]) == "tarball"
        assert progress.stage_for(["tar", "--acls", "-xf", "/out/r.tar.zst"]) == "unpack"

    def test_disk_preparation(self):
        assert progress.stage_for(["sgdisk", "--zap-all", "/dev/loop0"]) == "partition"
        assert progress.stage_for(["mkfs.ext4", "-F", "/dev/loop0p4"]) == "partition"

    def test_bootloader_work(self):
        assert progress.stage_for(["chroot", "/mnt", "grub-install", "/dev/loop0"]) == "bootloader"
        assert progress.stage_for(["chroot", "/mnt", "update-initramfs", "-u"]) == "bootloader"

    def test_an_unremarkable_command_belongs_to_no_stage(self):
        # mkdir and write-file happen throughout and must not yank the display
        # back to an earlier stage.
        assert progress.stage_for(["mkdir", "-p", "/x"]) is None
        assert progress.stage_for(["write-file", "/etc/fstab"]) is None
        assert progress.stage_for([]) is None


class TestTimeBasedFallback:
    """Stages with no percentage of their own still get a bar, after one build.

    tar and grub report no percentage. Once the timings cache knows how long
    they took last time, elapsed against that is a defensible estimate - and it
    is marked as an estimate in the UI rather than presented as measurement.
    """

    def setup_method(self):
        self.now = 0.0
        self.timeline = progress.Timeline(
            timings={"tarball": 100.0, "packages": 900.0},
            clock=lambda: self.now,
        )

    def test_uses_a_real_fraction_when_there_is_one(self):
        self.timeline.start("packages")
        self.timeline.update(0.25)
        assert self.timeline.displayed_fraction() == (0.25, False)

    def test_falls_back_to_elapsed_against_last_time(self):
        self.timeline.start("tarball")
        self.now += 50
        fraction, estimated = self.timeline.displayed_fraction()
        assert fraction == pytest.approx(0.5)
        assert estimated is True

    def test_an_overrunning_stage_never_reads_as_finished(self):
        # Showing 100% on a stage that is still working is how a progress bar
        # loses the operator's trust for the rest of the build.
        self.timeline.start("tarball")
        self.now += 500
        fraction, _ = self.timeline.displayed_fraction()
        assert fraction < 1.0

    def test_no_history_means_no_guess(self):
        timeline = progress.Timeline(clock=lambda: self.now)
        timeline.start("tarball")
        self.now += 50
        assert timeline.displayed_fraction() == (None, True)


class TestStagesOnlyMoveForward:
    def setup_method(self):
        self.now = 0.0
        self.timeline = progress.Timeline(clock=lambda: self.now)

    def test_a_finished_stage_does_not_restart(self):
        # losetup runs twice in a write: attaching the loop device before
        # partitioning and detaching it during cleanup. Both look like the
        # partition stage, and the second one arrives after the bootloader is
        # already installed.
        self.timeline.start("partition")
        self.now += 30
        self.timeline.start("bootloader")
        self.timeline.start("partition")
        assert self.timeline.current == "bootloader"

    def test_the_earlier_duration_survives_the_second_sighting(self):
        self.timeline.start("partition")
        self.now += 30
        self.timeline.start("bootloader")
        self.timeline.start("partition")
        assert self.timeline.durations()["partition"] == 30

    def test_a_repeated_stage_is_not_counted_twice_in_the_total(self):
        self.timeline.start("partition")
        self.timeline.start("bootloader")
        self.timeline.start("partition")
        self.timeline.finish()
        expected = self.timeline.weight_of("partition") + self.timeline.weight_of("bootloader")
        assert self.timeline.overall() == pytest.approx(expected)
