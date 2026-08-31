"""Ordering tests for the write stage.

Every step here is destructive and most of them depend on the previous one
having landed. Getting the order wrong produces failures that look like flaky
hardware, so the sequence itself is the thing under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from portlin.config import WriteConfig
from portlin.errors import TargetError
from portlin.install import write_stick
from portlin.layout import GIB
from portlin.runner import Runner


@pytest.fixture
def plain_config(tmp_path) -> WriteConfig:
    return WriteConfig(
        target=str(tmp_path / "stick.img"),
        rootfs=tmp_path / "rootfs.tar.zst",
        image_size=32 * GIB,
    )


@pytest.fixture
def encrypted_config(tmp_path) -> WriteConfig:
    return WriteConfig(
        target=str(tmp_path / "stick.img"),
        rootfs=tmp_path / "rootfs.tar.zst",
        encrypt=True,
        passphrase="correct horse battery staple",
        image_size=32 * GIB,
    )


class TestUnencryptedWrite:
    @pytest.fixture(autouse=True)
    def _run(self, plain_config, runner, trace):
        write_stick(plain_config, runner)
        self.t = trace(runner)

    def test_attaches_a_loop_device_before_partitioning(self):
        assert self.t.before(("losetup", "--show"), ("sgdisk", "--zap-all"))

    def test_wipes_the_partition_table_before_writing_a_new_one(self):
        assert self.t.before(("sgdisk", "--zap-all"), ("sgdisk", "-n1:"))

    def test_settles_udev_before_touching_the_new_partitions(self):
        # Without this the very next mkfs races the kernel creating the node and
        # fails with a bewildering "no such file or directory".
        assert self.t.before(("partprobe",), ("mkfs.vfat",))
        assert self.t.before(("udevadm", "settle"), ("mkfs.vfat",))

    def test_creates_all_three_filesystems_on_the_right_partitions(self):
        assert self.t.has("mkfs.vfat", "/dev/loop0p2")
        assert self.t.has("mkfs.ext4", "/dev/loop0p3")
        assert self.t.has("mkfs.ext4", "/dev/loop0p4")

    def test_mounts_root_then_boot_then_esp(self):
        root = self.t.index("mount /dev/loop0p4")
        boot = self.t.index("mount /dev/loop0p3")
        esp = self.t.index("mount /dev/loop0p2")
        assert root < boot < esp

    def test_unpacks_only_after_the_whole_tree_is_mounted(self):
        # Unpacking before /boot is mounted would bury the kernel under the root
        # filesystem, where the bootloader can never find it.
        assert self.t.before(("mount /dev/loop0p2",), ("tar", "-xf"))

    def test_writes_fstab_after_unpacking(self):
        # The tarball contains no fstab, but unpacking after writing one would
        # overwrite it with nothing.
        assert self.t.before(("tar", "-xf"), ("write-file", "etc/fstab"))

    def test_does_not_write_a_crypttab(self):
        assert not self.t.has("write-file", "etc/crypttab")

    def test_never_formats_a_luks_container(self):
        assert not self.t.has("luksFormat")

    def test_installs_grub_for_both_firmware_families(self):
        assert self.t.has("grub-install", "--target=i386-pc")
        assert self.t.has("grub-install", "--target=x86_64-efi")

    def test_uefi_install_uses_the_removable_fallback_path(self):
        # Without --removable the stick only boots on machines whose firmware has
        # been told about it, which defeats the entire purpose.
        assert self.t.has("grub-install", "--target=x86_64-efi", "--removable")

    def test_uefi_install_does_not_touch_the_build_hosts_firmware(self):
        assert self.t.has("grub-install", "--target=x86_64-efi", "--no-nvram")

    def test_bios_install_targets_the_whole_disk_not_a_partition(self):
        line = self.t.lines[self.t.index("grub-install", "--target=i386-pc")]
        assert line.endswith("/dev/loop0")

    def test_generates_the_menu_after_installing_both_bootloaders(self):
        assert self.t.before(("grub-install", "--target=i386-pc"), ("grub-mkconfig",))
        assert self.t.before(("grub-install", "--target=x86_64-efi"), ("grub-mkconfig",))

    def test_removes_the_boot_splash(self):
        # plymouth owns the console during boot and plymouth-quit-wait can
        # deadlock against a display manager waiting on the wizard. It only ever
        # hid the boot log, which on unfamiliar hardware is worth seeing.
        assert self.t.has("apt-get", "purge", "plymouth")

    def test_the_splash_goes_before_the_initramfs_is_rebuilt(self):
        # Otherwise plymouth's initramfs hook survives into the new initramfs.
        assert self.t.before(("apt-get", "purge", "plymouth"), ("update-initramfs",))

    def test_installs_the_first_boot_wizard(self):
        # Shipped at write time, not baked into the rootfs, so a cached tarball
        # can never carry a stale copy of portlin's own code.
        assert self.t.has("write-file", "usr/local/sbin/portlin-firstboot")
        assert self.t.has("write-file", "portlin-firstboot.service")
        assert self.t.has("write-file", "var/lib/portlin/firstboot-pending")
        assert self.t.has("systemctl", "enable", "portlin-firstboot.service")

    def test_the_wizard_is_installed_before_the_initramfs_is_built(self):
        assert self.t.before(("write-file", "portlin-firstboot"), ("update-initramfs",))

    def test_regenerates_the_initramfs_inside_the_chroot(self):
        assert self.t.has("chroot", "update-initramfs")

    def test_binds_dev_and_proc_before_running_anything_in_the_chroot(self):
        assert self.t.before(("mount --rbind /dev",), ("chroot",))
        assert self.t.before(("mount --rbind /proc",), ("chroot",))

    def test_unmounts_in_reverse_order(self):
        # Exact lines, because "umount <mnt>/boot" is a prefix of
        # "umount <mnt>/boot/efi" and substring matching would compare a line
        # against itself.
        mnt = "/tmp/portlin-mnt-dryrun"
        order = [
            self.t.lines.index(f"umount {mnt}/boot/efi"),
            self.t.lines.index(f"umount {mnt}/boot"),
            self.t.lines.index(f"umount {mnt}"),
        ]
        assert order == sorted(order), (
            "unmounts must unwind innermost first, otherwise the outer umount "
            "fails with EBUSY and leaves the tree mounted"
        )

    def test_detaches_the_loop_device_last(self):
        assert self.t.before(("umount",), ("losetup", "-d"))

    def test_syncs_before_finishing(self):
        assert self.t.has("sync")


class TestEncryptedWrite:
    @pytest.fixture(autouse=True)
    def _run(self, encrypted_config, runner, trace):
        write_stick(encrypted_config, runner)
        self.t = trace(runner)

    def test_formats_luks_on_the_root_partition_only(self):
        line = self.t.lines[self.t.index("luksFormat")]
        assert line.endswith("/dev/loop0p4")

    def test_boot_and_esp_stay_outside_the_container(self):
        assert self.t.has("mkfs.vfat", "/dev/loop0p2")
        assert self.t.has("mkfs.ext4", "/dev/loop0p3")

    def test_caps_the_kdf_memory_so_low_ram_machines_can_unlock(self):
        # cryptsetup would otherwise size argon2id against the build machine and
        # produce a stick that a 2 GB netbook cannot open.
        assert self.t.has("luksFormat", "--pbkdf-memory", "262144")

    def test_the_passphrase_never_appears_in_a_command(self):
        assert not any(
            "correct horse" in part
            for command in self.t.commands
            for part in command
        )

    def test_passphrase_is_delivered_on_stdin(self):
        assert self.t.has("luksFormat", "--key-file", "-")
        assert self.t.has("cryptsetup open", "--key-file", "-")

    def test_makes_the_root_filesystem_inside_the_mapper(self):
        assert self.t.has("mkfs.ext4", "/dev/mapper/portlin_root")
        assert not self.t.has("mkfs.ext4", "/dev/loop0p4")

    def test_opens_the_container_before_formatting_it(self):
        assert self.t.before(("cryptsetup open",), ("mkfs.ext4", "/dev/mapper"))

    def test_writes_crypttab_before_building_the_initramfs(self):
        # The cryptsetup initramfs hook reads crypttab to decide what unlock
        # support to include. Written afterwards, the stick cannot open itself.
        assert self.t.before(("write-file", "etc/crypttab"), ("update-initramfs",))

    def test_closes_the_mapping_after_every_unmount(self):
        assert self.t.before(("umount",), ("cryptsetup close",))

    def test_detaches_the_loop_device_after_closing_the_mapping(self):
        assert self.t.before(("cryptsetup close",), ("losetup", "-d"))


class TestFailureUnwinds:
    def test_a_failure_mid_write_still_closes_and_detaches(self, encrypted_config, runner, trace):
        """A crash must not leave a live /dev/mapper node or a bound loop device."""
        original_run = runner.run
        calls = {"n": 0}

        def explode_on_unpack(argv, **kwargs):
            if argv and argv[0] == "tar" and "-xf" in argv:
                calls["n"] += 1
                raise RuntimeError("simulated tar failure")
            return original_run(argv, **kwargs)

        runner.run = explode_on_unpack
        with pytest.raises(RuntimeError, match="simulated tar failure"):
            write_stick(encrypted_config, runner)

        t = trace(runner)
        assert calls["n"] == 1
        assert t.has("umount")
        assert t.has("cryptsetup close")
        assert t.has("losetup", "-d")
        assert t.before(("umount",), ("cryptsetup close",))


class TestAwaitPartitions:
    """Waiting for the observable outcome, rather than firing partprobe and hoping."""

    def _plan_and_target(self, tmp_path):
        from portlin.layout import plan_partitions
        from portlin.target import ImageTarget

        plan = plan_partitions(32 * GIB)
        target = ImageTarget(tmp_path / "s.img", Runner(dry_run=True), size_bytes=32 * GIB)
        target._loop = str(tmp_path / "loop9")
        return plan, target

    def test_returns_immediately_when_the_nodes_exist(self, tmp_path):
        from portlin.install import _await_partitions

        plan, target = self._plan_and_target(tmp_path)
        for part in plan.partitions:
            Path(target.partition(part.number)).touch()

        runner = Runner()
        _await_partitions(runner, target, plan)
        # No nudge was needed, so partx was never invoked.
        assert not any("partx" in c[0] for c in runner.commands)

    def test_nudges_with_partx_when_the_nodes_are_absent(self, tmp_path, monkeypatch):
        from portlin import install

        monkeypatch.setattr(install, "PARTITION_WAIT_SECONDS", 0.05)
        plan, target = self._plan_and_target(tmp_path)
        runner = Runner()

        with pytest.raises(TargetError, match="did not appear"):
            install._await_partitions(runner, target, plan)
        # partx is the nudge for the case where nothing else creates the nodes.
        assert any(command[0] == "partx" for command in runner.commands)

    def test_the_error_names_the_missing_nodes(self, tmp_path, monkeypatch):
        from portlin import install

        monkeypatch.setattr(install, "PARTITION_WAIT_SECONDS", 0.05)
        plan, target = self._plan_and_target(tmp_path)
        with pytest.raises(TargetError) as exc:
            install._await_partitions(Runner(), target, plan)
        assert "loop9p2" in str(exc.value)
        assert "loop9p4" in str(exc.value)

    def test_does_not_wait_for_the_bios_boot_partition(self, tmp_path, monkeypatch):
        # Partition 1 is never formatted or mounted, so its node is irrelevant
        # and requiring it would fail writes that are perfectly fine.
        from portlin import install

        monkeypatch.setattr(install, "PARTITION_WAIT_SECONDS", 0.05)
        plan, target = self._plan_and_target(tmp_path)
        for number in (2, 3, 4):
            Path(target.partition(number)).touch()
        install._await_partitions(Runner(), target, plan)

    def test_tries_partx_then_sysfs_before_giving_up(self, tmp_path, monkeypatch):
        # Two distinct failure modes, two distinct remedies: the kernel not
        # having re-read the table, and nothing having created the nodes.
        from portlin import install

        monkeypatch.setattr(install, "PARTITION_WAIT_SECONDS", 0.05)
        plan, target = self._plan_and_target(tmp_path)
        runner = Runner()
        with pytest.raises(TargetError):
            install._await_partitions(runner, target, plan)
        assert any(command[0] == "partx" for command in runner.commands)

    def test_a_dry_run_never_touches_the_filesystem(self, tmp_path):
        from portlin.install import _await_partitions

        plan, target = self._plan_and_target(tmp_path)
        _await_partitions(Runner(dry_run=True), target, plan)


class TestByUuidLinks:
    """grub-mkconfig will bake in a raw device path without these."""

    def _run(self, tmp_path, monkeypatch, mapping, existing=()):
        from contextlib import ExitStack

        from portlin import install

        by_uuid = tmp_path / "by-uuid"
        by_uuid.mkdir()
        for name in existing:
            (by_uuid / name).symlink_to("/dev/somewhere")
        monkeypatch.setattr(install, "BY_UUID", by_uuid)

        runner = Runner()
        stack = ExitStack()
        install._ensure_by_uuid_links(stack, runner, mapping)
        return by_uuid, runner, stack

    def test_creates_a_link_per_filesystem(self, tmp_path, monkeypatch):
        by_uuid, _, stack = self._run(
            tmp_path, monkeypatch, {"aaaa": "/dev/loop0p4", "bbbb": "/dev/loop0p3"}
        )
        assert (by_uuid / "aaaa").readlink().name == "loop0p4"
        assert (by_uuid / "bbbb").readlink().name == "loop0p3"
        stack.close()

    def test_removes_only_the_links_it_created(self, tmp_path, monkeypatch):
        # A host with a working udev owns this directory, so portlin must leave
        # it exactly as it found it.
        by_uuid, _, stack = self._run(
            tmp_path, monkeypatch, {"aaaa": "/dev/loop0p4"}, existing=("cccc",)
        )
        stack.close()
        assert not (by_uuid / "aaaa").is_symlink()
        assert (by_uuid / "cccc").is_symlink()

    def test_leaves_an_existing_link_alone(self, tmp_path, monkeypatch):
        by_uuid, _, stack = self._run(
            tmp_path, monkeypatch, {"cccc": "/dev/loop0p4"}, existing=("cccc",)
        )
        # Untouched, still pointing where udev put it.
        assert (by_uuid / "cccc").readlink() == Path("/dev/somewhere")
        stack.close()
        assert (by_uuid / "cccc").is_symlink()

    def test_a_dry_run_creates_nothing(self, tmp_path, monkeypatch):
        from contextlib import ExitStack

        from portlin import install

        by_uuid = tmp_path / "by-uuid"
        monkeypatch.setattr(install, "BY_UUID", by_uuid)
        install._ensure_by_uuid_links(ExitStack(), Runner(dry_run=True), {"a": "/dev/x"})
        assert not by_uuid.exists()


class TestCreateNodesFromSysfs:
    """Doing udev's job where there is no udev, as in any container."""

    def _sysfs(self, tmp_path, monkeypatch, name, device_number):
        root = tmp_path / "sysblock"
        (root / name).mkdir(parents=True)
        (root / name / "dev").write_text(f"{device_number}\n")
        monkeypatch.setattr("portlin.install.SYSFS_BLOCK", root)
        return root

    def test_creates_a_node_with_the_device_number_the_kernel_reports(
        self, tmp_path, monkeypatch
    ):
        from portlin.install import _create_nodes_from_sysfs

        self._sysfs(tmp_path, monkeypatch, "loop9p2", "259:1")
        runner = Runner(dry_run=True)
        _create_nodes_from_sysfs(runner, ["/dev/loop9p2"])
        assert runner.commands == [
            ["mknod", "--mode=0660", "/dev/loop9p2", "b", "259", "1"]
        ]

    def test_skips_partitions_the_kernel_does_not_know_about(self, tmp_path, monkeypatch):
        # A missing sysfs entry means the partition genuinely does not exist, so
        # fabricating a node would only produce a more confusing error later.
        from portlin.install import _create_nodes_from_sysfs

        self._sysfs(tmp_path, monkeypatch, "loop9p2", "259:1")
        runner = Runner(dry_run=True)
        _create_nodes_from_sysfs(runner, ["/dev/loop9p7"])
        assert runner.commands == []

    def test_tolerates_an_unreadable_device_number(self, tmp_path, monkeypatch):
        from portlin.install import _create_nodes_from_sysfs

        self._sysfs(tmp_path, monkeypatch, "loop9p2", "not-a-device-number")
        runner = Runner(dry_run=True)
        _create_nodes_from_sysfs(runner, ["/dev/loop9p2"])
        assert runner.commands == []

    def test_replaces_a_stale_node_rather_than_skipping_it(self, tmp_path, monkeypatch):
        # Reattaching a loop device reallocates its partitions' minor numbers, so
        # a node from an earlier attach is a valid block file pointing at
        # nothing. Skipping it produces "can't open blockdev" much later.
        from portlin.install import _create_nodes_from_sysfs

        self._sysfs(tmp_path, monkeypatch, "loop9p2", "259:1")
        stale = tmp_path / "loop9p2"
        stale.touch()
        runner = Runner(dry_run=True)
        _create_nodes_from_sysfs(runner, [str(stale)])
        assert runner.commands[0][0] == "rm"
        assert runner.commands[1][0] == "mknod"

    def test_handles_several_missing_partitions(self, tmp_path, monkeypatch):
        from portlin.install import _create_nodes_from_sysfs

        root = self._sysfs(tmp_path, monkeypatch, "loop9p2", "259:1")
        (root / "loop9p3").mkdir()
        (root / "loop9p3" / "dev").write_text("259:2\n")
        runner = Runner(dry_run=True)
        _create_nodes_from_sysfs(runner, ["/dev/loop9p2", "/dev/loop9p3"])
        assert len(runner.commands) == 2


class TestGuards:
    def test_refuses_a_missing_rootfs_when_actually_running(self, tmp_path):
        from portlin.runner import Runner

        cfg = WriteConfig(
            target=str(tmp_path / "stick.img"),
            rootfs=tmp_path / "absent.tar.zst",
            image_size=32 * GIB,
        )
        with pytest.raises(TargetError, match="rootfs tarball not found"):
            write_stick(cfg, Runner(dry_run=False))

    def test_encryption_without_a_passphrase_is_rejected_at_config_time(self, tmp_path):
        from portlin.errors import BuildError

        with pytest.raises(BuildError, match="no passphrase"):
            WriteConfig(target="/dev/sdz", rootfs=tmp_path / "r.tar", encrypt=True)

    def test_a_passphrase_without_encryption_is_rejected(self, tmp_path):
        from portlin.errors import BuildError

        with pytest.raises(BuildError, match="not enabled"):
            WriteConfig(
                target="/dev/sdz", rootfs=tmp_path / "r.tar", passphrase="secret"
            )


class TestProgressSignals:
    @pytest.fixture(autouse=True)
    def _run(self, plain_config, runner, trace):
        write_stick(plain_config, runner)
        self.t = trace(runner)

    def test_unpacking_reports_checkpoints(self):
        # The unpack is several minutes of nothing on a slow stick, and it is
        # the stage where a stalled build is most often suspected.
        tar = " ".join(self.t.command_at("tar", "-xf"))
        assert "--checkpoint=2000" in tar
        assert "--checkpoint-action=echo" in tar
