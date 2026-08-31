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


class TestDarkTheme:
    """A "dark theme" is not one setting; it is four subsystems agreeing.

    Each of these files covers a surface the others cannot reach, so the tests
    are mostly about the seams: a light title bar around a dark window, or a
    white login screen ahead of a dark session, is what a half-applied theme
    looks like in practice.
    """

    def test_xsettings_names_a_theme_that_ships_a_dark_variant(self):
        rendered = templates.render_xsettings_channel()
        assert 'name="ThemeName" type="string" value="Greybird-dark"' in rendered

    def test_xsettings_is_a_valid_xfconf_channel_document(self):
        rendered = templates.render_xsettings_channel()
        assert rendered.startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert '<channel name="xsettings" version="1.0">' in rendered

    def test_xsettings_asks_gtk_apps_for_their_dark_variant(self):
        # Reaches applications that pick a variant themselves rather than
        # following the theme name, which is most GTK3 apps with a header bar.
        rendered = templates.render_xsettings_channel()
        assert 'name="ApplicationPreferDarkTheme" type="bool" value="true"' in rendered

    def test_window_decorations_match_the_desktop(self):
        # xfwm4 has its own theme setting. Left alone it stays light, which
        # produces dark windows wearing light title bars.
        rendered = templates.render_xfwm4_channel()
        assert '<channel name="xfwm4" version="1.0">' in rendered
        assert 'name="theme" type="string" value="Greybird-dark"' in rendered

    def test_gtk3_names_the_theme_for_apps_started_outside_the_session(self):
        rendered = templates.render_gtk3_settings()
        assert "[Settings]" in rendered
        assert "gtk-theme-name=Greybird-dark" in rendered
        assert "gtk-application-prefer-dark-theme=1" in rendered

    def test_gtk4_asks_for_dark_without_naming_the_theme(self):
        # Greybird-dark ships gtk-2.0 and gtk-3.0 assets but no gtk-4.0 ones.
        # Naming it here sends GTK4 looking for files that do not exist and
        # silently drops it back to the light built-in.
        rendered = templates.render_gtk4_settings()
        assert "gtk-application-prefer-dark-theme=1" in rendered
        assert "Greybird" not in rendered

    def test_the_login_screen_is_dark_too(self):
        # Otherwise the stick flashes a white greeter before every dark session.
        rendered = templates.render_lightdm_greeter_conf()
        assert "[greeter]" in rendered
        assert "theme-name=Greybird-dark" in rendered

    def test_the_terminal_does_not_follow_the_gtk_theme(self):
        # xfce4-terminal stores its own colours and defaults to a light
        # background no matter how dark the desktop around it is.
        rendered = templates.render_terminal_config()
        assert "[Configuration]" in rendered
        assert "ColorBackground=#12161C" in rendered
        assert "ColorForeground=#E8EDF3" in rendered

    def test_the_terminal_palette_is_sixteen_colours(self):
        # xfce4-terminal reads the palette positionally; a short list leaves the
        # remaining ANSI colours at their light-scheme defaults.
        line = next(
            l for l in templates.render_terminal_config().splitlines()
            if l.startswith("ColorPalette=")
        )
        colours = line.split("=", 1)[1].split(";")
        assert len(colours) == 16
        assert all(re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in colours)

    def test_every_rendered_file_says_where_it_came_from(self):
        for rendered in (
            templates.render_gtk3_settings(),
            templates.render_gtk4_settings(),
            templates.render_lightdm_greeter_conf(),
            templates.render_terminal_config(),
        ):
            assert "portlin" in rendered.splitlines()[0]
