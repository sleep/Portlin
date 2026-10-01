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

    def test_says_which_of_the_two_runs_a_line_is_from(self):
        assert progress.parse_apt_status("dlstatus:1:12.5:Retrieving file 1 of 8").kind == "dlstatus"
        assert progress.parse_apt_status("pmstatus:x:41.7:Unpacking x").kind == "pmstatus"


class TestInstallFraction:
    """One apt-get install is a download and then dpkg, each counted 0-100%."""

    def fraction(self, line):
        return progress.apt_install_fraction(progress.parse_apt_status(line))

    def test_the_bar_does_not_empty_when_dpkg_starts(self):
        # Taken raw, a finished download read as a finished install, and dpkg's
        # first line then sent the bar back to nothing and the ETA to hours.
        downloaded = self.fraction("dlstatus:927:100:Retrieving file 927 of 927")
        started = self.fraction("pmstatus:dpkg-exec:0:Running dpkg")
        assert started >= downloaded

    def test_the_download_moves_the_bar(self):
        halfway = self.fraction("dlstatus:449:50:Retrieving file 449 of 927")
        assert 0 < halfway < self.fraction("pmstatus:dpkg-exec:0:Running dpkg")

    def test_runs_from_empty_to_full(self):
        assert self.fraction("dlstatus:1:0:Retrieving file 1 of 927") == 0.0
        assert self.fraction("pmstatus:x:100:Installed x") == pytest.approx(1.0)


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

    def test_an_infinite_duration_is_dropped(self, tmp_path):
        # json reads Infinity, and an infinite ETA cannot be printed: the
        # display would raise mid-build over a cache that exists to be cosmetic.
        path = tmp_path / "timings.json"
        path.write_text('{"packages": Infinity, "unpack": 12.0}')
        assert progress.load_timings(path) == {"unpack": 12.0}


class TestProfileCache:
    STEPS = progress.PROFILE_STEPS

    def profile(self, end=100.0):
        return [end * step / self.STEPS for step in range(self.STEPS + 1)]

    def test_round_trips_beside_the_durations(self, tmp_path):
        path = tmp_path / "timings.json"
        progress.save_timings(path, {"packages": 100.0}, {"packages": self.profile()})
        assert progress.load_profiles(path) == {"packages": self.profile()}
        # And the durations read exactly as before, profiles or not.
        assert progress.load_timings(path) == {"packages": 100.0}

    def test_a_cache_from_before_profiles_has_none(self, tmp_path):
        path = tmp_path / "timings.json"
        path.write_text(json.dumps({"packages": 100.0}))
        assert progress.load_profiles(path) == {}

    def test_a_profile_of_the_wrong_length_is_dropped(self, tmp_path):
        # Recorded with a different PROFILE_STEPS, every step would be read as
        # some other step.
        path = tmp_path / "timings.json"
        path.write_text(json.dumps({"profiles": {"packages": self.profile()[:-1]}}))
        assert progress.load_profiles(path) == {}

    def test_a_profile_that_goes_back_in_time_is_dropped(self, tmp_path):
        broken = self.profile()
        broken[10], broken[11] = broken[11], broken[10]
        path = tmp_path / "timings.json"
        path.write_text(json.dumps({"profiles": {"packages": broken, "unpack": self.profile()}}))
        assert progress.load_profiles(path) == {"unpack": self.profile()}

    def test_junk_is_dropped_rather_than_trusted(self, tmp_path):
        path = tmp_path / "timings.json"
        path.write_text(json.dumps({"profiles": {"a": "soon", "b": [None] * (self.STEPS + 1)}}))
        assert progress.load_profiles(path) == {}
        path.write_text(json.dumps({"profiles": [1, 2, 3]}))
        assert progress.load_profiles(path) == {}


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


class TestProfileRecording:
    def setup_method(self):
        self.now = 0.0
        self.timeline = progress.Timeline(clock=lambda: self.now)

    def test_records_when_each_step_was_first_reached(self):
        self.timeline.start("packages")
        self.now = 10
        self.timeline.update(0.0)
        self.now = 30
        self.timeline.update(0.5)
        self.now = 50
        self.timeline.update(1.0)
        self.timeline.finish()
        profile = self.timeline.profiles()["packages"]
        assert profile[0] == 10
        assert profile[progress.PROFILE_STEPS // 2] == 30
        assert profile[-1] == 50

    def test_steps_never_reached_count_as_reached_at_the_end(self):
        # apt's last line is 99.97%, and the stage goes on past it anyway, so
        # the end of the stage is when the last steps were really done.
        self.timeline.start("packages")
        self.timeline.update(0.9997)
        self.now = 80
        self.timeline.finish()
        assert self.timeline.profiles()["packages"][-1] == 80

    def test_a_fraction_that_slips_back_does_not_rewrite_the_profile(self):
        # apt recomputes its totals mid-install and the percentage dips.
        self.timeline.start("packages")
        self.timeline.update(0.5)
        self.now = 10
        self.timeline.update(0.49)
        self.now = 20
        self.timeline.update(0.5)
        self.timeline.finish()
        assert self.timeline.profiles()["packages"][progress.PROFILE_STEPS // 2] == 0

    def test_a_stage_with_no_percentage_has_no_profile(self):
        self.timeline.start("tarball")
        self.now = 10
        self.timeline.finish()
        assert "tarball" not in self.timeline.profiles()


class TestCalibratedEta:
    """With an earlier build to go on, the ETA keeps its clock, not apt's.

    apt counts steps rather than time. It reaches 99% with the initramfs still
    to build, sits at 81% through the kernel's postinst and reports nothing
    through apt update, so an ETA extrapolated from its percentage raced to
    zero minutes before the stage was done. The same stalls fall at the same
    percentages every build, so the last build's timing of each step is the
    clock, and the percentage only corrects it.
    """

    # The packages stage in miniature, as (seconds, fraction at the end):
    # apt update with no percentage, a steady stretch, the kernel's postinst
    # holding at 80%, the rest, then the initramfs trigger after the last line.
    STAGE = [(20, None), (100, 0.8), (60, 0.8), (20, 1.0), (40, 1.0)]

    def setup_method(self):
        self.now = 0.0
        previous = progress.Timeline(clock=lambda: self.now)
        self.play(previous, self.STAGE)
        self.timings = previous.durations()
        self.profiles = previous.profiles()

    def play(self, timeline, stage, watch=None):
        """Run one stage a second at a time, as apt would report it."""
        timeline.start("packages")
        fraction = None
        for seconds, target in stage:
            begin = fraction
            for tick in range(1, seconds + 1):
                self.now += 1
                if target is not None and target != begin:
                    start = begin or 0.0
                    timeline.update(start + (target - start) * tick / seconds)
                if watch:
                    watch(timeline)
            if target is not None:
                fraction = target
        timeline.finish()

    def etas(self, stage):
        """(true seconds left, ETA shown) for every second of this build."""
        timeline = progress.Timeline(
            timings=self.timings, profiles=self.profiles, clock=lambda: self.now
        )
        total = sum(seconds for seconds, _ in stage)
        seen = []
        self.play(
            timeline,
            stage,
            watch=lambda t: seen.append((total - t.elapsed(), t.stage_remaining())),
        )
        return seen

    def test_a_rebuild_like_the_last_counts_down_in_real_time(self):
        # Through the stall at 80% and the tail at 100% alike, where the old
        # extrapolation stood still and then showed nothing left.
        for true, eta in self.etas(self.STAGE):
            assert eta == pytest.approx(true, abs=1)

    def test_falling_behind_early_does_not_skip_the_tail(self):
        # A slow download puts every later step behind the last build's clock.
        # Timed from the start of the stage, the build was then already past
        # the stall and the trigger and the ETA read zero through both.
        behind = [(20, None), (200, 0.8), (60, 0.8), (20, 1.0), (40, 1.0)]
        for true, eta in self.etas(behind)[-120:]:
            assert eta == pytest.approx(true, abs=1)

    def test_getting_ahead_brings_the_eta_forward(self):
        ahead = [(20, None), (50, 0.8), (60, 0.8), (20, 1.0), (40, 1.0)]
        for true, eta in self.etas(ahead)[70:]:
            assert eta == pytest.approx(true, abs=1)

    def test_before_progress_begins_it_waits_where_the_last_build_began(self):
        # A slow apt update has not got the build anywhere: it holds at
        # however long the rest took last time rather than counting past it.
        late = [(50, None), (100, 0.8), (60, 0.8), (20, 1.0), (40, 1.0)]
        etas = [eta for _, eta in self.etas(late)]
        rest = self.timings["packages"] - self.profiles["packages"][0]
        assert etas[29] == pytest.approx(rest)
        assert etas[49] == pytest.approx(rest)

    def test_a_duration_without_a_profile_counts_down_from_it(self):
        # A cache written before profiles existed, or a stage with no
        # percentage of its own.
        timeline = progress.Timeline(timings={"packages": 240.0}, clock=lambda: self.now)
        timeline.start("packages")
        self.now += 100
        timeline.update(0.95)
        assert timeline.stage_remaining() == pytest.approx(140)


class TestFirstBuildEta:
    def test_measures_the_rate_from_when_progress_began(self):
        # A minute of apt update before the percentage moves used to count as
        # time spent getting to it, which put the ETA far too high early and
        # had it fall several seconds per second afterwards.
        now = [0.0]
        timeline = progress.Timeline(clock=lambda: now[0])
        timeline.start("packages")
        now[0] = 60
        timeline.update(0.0)
        now[0] = 80
        timeline.update(0.5)
        assert timeline.stage_remaining() == pytest.approx(20)

    def test_has_no_estimate_before_progress_begins(self):
        timeline = progress.Timeline(clock=lambda: 30.0)
        timeline.start("packages")
        assert timeline.stage_remaining() is None


class TestTotalEta:
    TIMINGS = {stage.key: 100.0 for stage in progress.DEFAULT_STAGES}

    def test_adds_the_stages_still_to_come_to_the_current_one(self):
        now = [0.0]
        timeline = progress.Timeline(timings=self.TIMINGS, clock=lambda: now[0])
        timeline.start("debootstrap")
        now[0] = 100
        timeline.start("packages")
        now[0] = 130
        later = len(progress.DEFAULT_STAGES) - 2
        assert timeline.remaining() == pytest.approx(70 + 100 * later)

    def test_keeps_falling_through_a_stage_with_no_percentage(self):
        # Extrapolated from the overall fraction, which tar does not move, the
        # total ETA climbed for as long as the tarball took.
        now = [0.0]
        timeline = progress.Timeline(timings=self.TIMINGS, clock=lambda: now[0])
        timeline.start("tarball")
        now[0] = 10
        before = timeline.remaining()
        now[0] = 50
        assert timeline.remaining() == pytest.approx(before - 40)

    def test_is_unknown_on_a_first_build(self):
        timeline = progress.Timeline(clock=lambda: 0.0)
        timeline.start("debootstrap")
        assert timeline.remaining() is None

    def test_is_nothing_once_every_stage_is_done(self):
        timeline = progress.Timeline(timings=self.TIMINGS, clock=lambda: 0.0)
        for stage in progress.DEFAULT_STAGES:
            timeline.start(stage.key)
        timeline.finish()
        assert timeline.remaining() == 0
