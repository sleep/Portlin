"""Ordering and content tests for the build stage.

The critical property being asserted is negative: the tarball must contain no
identity and no target-specific configuration. Anything host-specific baked in
here silently destroys the portability the whole tool exists to provide.
"""

from __future__ import annotations

import pytest

from portlin.config import BuildConfig
from portlin.packages import BOOTSTRAP_INCLUDE
from portlin.rootfs import build_rootfs


@pytest.fixture
def config(tmp_path) -> BuildConfig:
    return BuildConfig(output=tmp_path / "rootfs.tar.zst", work_dir=tmp_path / "work")


@pytest.fixture
def built(config, runner, trace):
    build_rootfs(config, runner)
    return trace(runner)


class TestBootstrap:
    def test_debootstraps_amd64_regardless_of_the_build_host(self, built):
        assert built.has_tokens("debootstrap", "--arch=amd64")

    def test_enables_non_free_firmware(self, built):
        # Without this component the stick boots on unknown hardware and then
        # discovers it has no wifi driver, which is the most common way a
        # portable Linux install turns out to be useless.
        assert "non-free-firmware" in " ".join(built.command_at("debootstrap"))

    def test_seeds_the_bootstrap_with_the_minimum_apt_needs(self, built):
        line = " ".join(built.command_at("debootstrap"))
        for package in BOOTSTRAP_INCLUDE:
            assert package in line

    def test_writes_sources_list_before_the_first_apt_run(self, built):
        assert built.index("write-file", "etc/apt/sources.list") < built.token_index("apt-get", "update")

    def test_updates_before_installing(self, built):
        assert built.tokens_before(("apt-get", "update"), ("apt-get", "install"))


class TestChrootHygiene:
    def test_binds_the_kernel_filesystems_before_running_apt(self, built):
        for source in ("/dev", "/proc", "/sys", "/run"):
            assert built.index(f"mount --rbind {source}") < built.token_index("apt-get")

    def test_blocks_daemon_starts_during_the_build(self, built):
        # Without policy-rc.d a maintainer script tries to start a service against
        # the build host's init, which either fails the install or, worse, works.
        assert built.index("write-file", "policy-rc.d") < built.token_index("apt-get", "install")

    def test_removes_policy_rc_d_afterwards(self, built):
        assert built.has("rm -f", "policy-rc.d")

    def test_unmounts_everything_before_packing(self, built):
        assert built.token_index("umount") < built.token_index("tar", "-cf")


class TestImageConfiguration:
    def test_does_not_bake_in_the_wizard(self, built):
        # The wizard ships at write time instead, so a cached rootfs can never
        # carry a stale copy of portlin's own code.
        assert not built.has("write-file", "usr/local/sbin/portlin-firstboot")
        assert not built.has_tokens("systemctl", "enable", "portlin-firstboot.service")

    def test_configures_the_initramfs_for_unknown_hardware(self, built):
        assert built.has("write-file", "etc/initramfs-tools/conf.d/portlin")

    def test_ships_the_grub_defaults(self, built):
        assert built.has("write-file", "etc/default/grub")

    def test_does_not_stamp_the_version_at_build_time(self, built):
        # The tarball is reusable for months, so a version stamped into it would
        # describe the tarball rather than the stick written from it.
        assert not built.has("write-file", "etc/portlin-release")


class TestAnonymisation:
    def test_empties_machine_id_so_clones_are_not_twins(self, built):
        assert built.has(": > /etc/machine-id")

    def test_removes_ssh_host_keys(self, built):
        assert built.has("rm -f /etc/ssh/ssh_host_")

    def test_clears_the_apt_cache_before_packing(self, built):
        assert built.tokens_before(("apt-get", "clean"), ("tar", "-cf"))
        assert built.index("rm -rf /var/lib/apt/lists") < built.token_index("tar", "-cf")

    def test_writes_no_fstab(self, built):
        # fstab is target-specific: it names the UUIDs of one particular stick.
        # Baking one into the reusable tarball would make every stick built from
        # it try to mount the first stick's partitions.
        assert not built.has("write-file", "etc/fstab")

    def test_writes_no_crypttab(self, built):
        assert not built.has("write-file", "etc/crypttab")

    def test_installs_no_bootloader(self, built):
        # There is no device to install one onto at this stage.
        assert not built.has_tokens("grub-install")


class TestPacking:
    def test_preserves_numeric_ownership(self, built):
        # The build host's /etc/passwd is irrelevant to the image; mapping names
        # through it would assign files to whichever users happen to exist here.
        assert built.has_tokens("tar", "--numeric-owner")

    def test_preserves_extended_attributes(self, built):
        # File capabilities on binaries such as ping live in xattrs and vanish
        # silently without this, leaving a system with subtly broken tools.
        assert built.has_tokens("tar", "--xattrs")
        assert built.has_tokens("tar", "--acls")

    def test_packs_last(self, built):
        assert built.token_index("tar", "-cf") == len(built.lines) - 1


class TestPackageSelection:
    def test_a_minimal_build_installs_no_desktop(self, tmp_path, runner, trace):
        cfg = BuildConfig(
            output=tmp_path / "r.tar.zst",
            work_dir=tmp_path / "w",
            groups=["boot", "system"],
        )
        build_rootfs(cfg, runner)
        installed = trace(runner).command_at("apt-get", "install")
        assert "xfce4" not in installed
        assert "linux-image-amd64" in installed

    def test_extra_packages_reach_apt(self, tmp_path, runner, trace):
        cfg = BuildConfig(
            output=tmp_path / "r.tar.zst",
            work_dir=tmp_path / "w",
            groups=["boot"],
            extra_packages=["tmux"],
        )
        build_rootfs(cfg, runner)
        assert "tmux" in trace(runner).command_at("apt-get", "install")


class TestProgressSignals:
    """The build has to be able to say how far along it is.

    Both of these are options on commands that would work fine without them.
    They exist so that something watching the output can compute a real
    percentage instead of a phase-level guess.
    """

    def test_apt_emits_a_machine_readable_status_stream(self, built):
        # Pointed at stdout, which is already being read, so no extra pipe or
        # pass_fds is needed to collect it.
        install = built.command_at("apt-get", "install")
        assert "APT::Status-Fd=1" in " ".join(install)

    def test_apt_does_not_also_draw_its_own_progress(self, built):
        # -q suppresses apt's terminal rendering, which is meaningless on a pipe
        # and would otherwise be noise in the log pane.
        assert "-q" in built.command_at("apt-get", "install")

    def test_packing_reports_checkpoints(self, built):
        tar = " ".join(built.command_at("tar", "-cf"))
        assert "--checkpoint=2000" in tar
        assert "--checkpoint-action=echo" in tar
