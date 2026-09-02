"""These files decide whether a stick boots on a machine other than the one that
made it, so they are asserted on quite literally."""

from __future__ import annotations

import re

from portlin import templates


class TestFstab:
    def setup_method(self):
        self.fstab = templates.render_fstab(
            root_uuid="11111111-1111-1111-1111-111111111111",
            boot_uuid="22222222-2222-2222-2222-222222222222",
            esp_uuid="ABCD-1234",
        )

    def test_every_mount_is_identified_by_uuid(self):
        entries = [
            line for line in self.fstab.splitlines()
            if line and not line.startswith("#") and not line.startswith("tmpfs")
        ]
        assert entries, "expected some real entries"
        assert all(line.startswith("UUID=") for line in entries)

    def test_never_references_a_kernel_device_path(self):
        # /dev/sda4 is correct on exactly one machine. Its presence here would be
        # the single most likely cause of an unbootable stick.
        assert not re.search(r"/dev/(sd|nvme|mmcblk|loop)", self.fstab)

    def test_mounts_root_boot_and_esp(self):
        assert "\t/\text4\t" in self.fstab
        assert "\t/boot\text4\t" in self.fstab
        assert "\t/boot/efi\tvfat\t" in self.fstab

    def test_root_is_checked_first_and_boot_second(self):
        root = next(l for l in self.fstab.splitlines() if "\t/\text4" in l)
        boot = next(l for l in self.fstab.splitlines() if "\t/boot\text4" in l)
        assert root.endswith("0 1")
        assert boot.endswith("0 2")

    def test_root_reduces_flash_wear(self):
        root = next(l for l in self.fstab.splitlines() if "\t/\text4" in l)
        assert "noatime" in root
        assert "commit=120" in root

    def test_esp_is_not_world_readable(self):
        esp = next(l for l in self.fstab.splitlines() if "/boot/efi" in l)
        assert "umask=0077" in esp


class TestCrypttab:
    def test_references_the_luks_container_by_uuid(self):
        rendered = templates.render_crypttab(luks_uuid="dead-beef")
        assert "portlin_root\tUUID=dead-beef\tnone\tluks" in rendered

    def test_discard_is_off_by_default(self):
        assert "discard" not in templates.render_crypttab(luks_uuid="x")

    def test_discard_can_be_enabled(self):
        assert "luks,discard" in templates.render_crypttab(luks_uuid="x", discard=True)

    def test_the_passphrase_stash_is_off_by_default(self):
        # A stick past first boot has no use for the stash, and the keyscript is
        # what puts the passphrase in /run at all. Absent by default means a
        # settled stick caches nothing.
        assert "keyscript" not in templates.render_crypttab(luks_uuid="x")

    def test_the_written_crypttab_enables_the_stash(self):
        # The wiring install.py depends on: a first boot with no keyscript in
        # crypttab is a first boot that asks for the passphrase twice.
        import inspect

        from portlin import install

        source = inspect.getsource(install)
        call = source[source.index("render_crypttab("):]
        assert "stash_passphrase=True" in call[: call.index(")")]

    def test_the_stash_keyscript_can_be_enabled(self):
        # The initramfs cryptroot script pipes this keyscript's stdout into
        # cryptsetup, and its hook copy_execs whatever keyscript= names, so the
        # option is both how the stash runs and how it reaches the initramfs.
        rendered = templates.render_crypttab(luks_uuid="x", stash_passphrase=True)
        assert "keyscript=/lib/cryptsetup/scripts/portlin-stash-passphrase" in rendered


class TestDefaultGrub:
    def test_os_prober_is_disabled(self):
        # With os-prober on, the generated menu describes the build host's
        # operating systems and grub-mkconfig mounts disks that are none of its
        # business. On a stick meant for other people's machines that is fatal.
        assert "GRUB_DISABLE_OS_PROBER=true" in templates.render_default_grub()

    def test_cryptodisk_is_off_because_boot_is_plaintext(self):
        assert "GRUB_ENABLE_CRYPTODISK=n" in templates.render_default_grub()

    def test_preloads_both_partition_table_modules(self):
        assert 'GRUB_PRELOAD_MODULES="part_gpt part_msdos"' in templates.render_default_grub()


class TestInitramfsConf:
    def test_includes_every_driver_not_just_the_build_hosts(self):
        assert "MODULES=most" in templates.render_initramfs_conf()

    def test_disables_resume_probing(self):
        assert "RESUME=none" in templates.render_initramfs_conf()


class TestCryptsetupHookConf:
    def test_ships_the_keymap_so_the_passphrase_is_typeable(self):
        assert "KEYMAP=y" in templates.render_cryptsetup_hook_conf()


class TestSourcesList:
    def test_stable_suite_gets_updates_and_security(self):
        rendered = templates.render_sources_list(
            suite="trixie",
            mirror="http://deb.debian.org/debian",
            security_mirror="http://security.debian.org/debian-security",
            components="main contrib non-free-firmware",
        )
        assert "trixie main contrib non-free-firmware" in rendered
        assert "trixie-updates" in rendered
        assert "trixie-security" in rendered

    def test_sid_has_no_updates_or_security_pockets(self):
        # Emitting them anyway would make every apt run in the chroot fail.
        rendered = templates.render_sources_list(
            suite="sid",
            mirror="http://deb.debian.org/debian",
            security_mirror="http://security.debian.org/debian-security",
            components="main",
        )
        assert "sid-updates" not in rendered
        assert "sid-security" not in rendered

    def test_non_free_firmware_is_carried_through(self):
        rendered = templates.render_sources_list(
            suite="trixie",
            mirror="http://m",
            security_mirror="http://s",
            components="main contrib non-free-firmware",
        )
        assert "non-free-firmware" in rendered
