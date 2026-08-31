from __future__ import annotations

import pytest

from portlin import packages
from portlin.errors import BuildError


class TestResolve:
    def test_defaults_to_every_group(self):
        resolved = packages.resolve()
        assert "xfce4" in resolved
        assert "linux-image-amd64" in resolved
        assert "firefox-esr" in resolved

    def test_returns_a_sorted_deduplicated_list(self):
        resolved = packages.resolve()
        assert resolved == sorted(set(resolved))

    def test_ca_certificates_appears_once_despite_being_in_two_groups(self):
        assert packages.resolve().count("ca-certificates") == 1

    def test_a_named_group_selects_only_that_group(self):
        resolved = packages.resolve(["boot"])
        assert "linux-image-amd64" in resolved
        assert "xfce4" not in resolved

    def test_extra_packages_are_added(self):
        assert "tmux" in packages.resolve(["boot"], extra=["tmux"])

    def test_excluded_packages_are_removed(self):
        assert "firefox-esr" not in packages.resolve(exclude=["firefox-esr"])

    def test_exclude_wins_over_extra(self):
        resolved = packages.resolve(["boot"], extra=["tmux"], exclude=["tmux"])
        assert "tmux" not in resolved

    def test_an_unknown_group_fails_with_the_valid_options(self):
        with pytest.raises(BuildError) as exc:
            packages.resolve(["desktop", "nonsense"])
        assert "nonsense" in str(exc.value)
        assert "desktop" in str(exc.value)

    def test_minimal_groups_are_all_real_groups(self):
        assert all(g in packages.GROUPS for g in packages.MINIMAL_GROUPS)


class TestPortabilityRequirements:
    def test_ships_microcode_for_both_cpu_vendors(self):
        # The stick has no idea whose CPU it will wake up on, and each package is
        # inert on the other vendor's hardware.
        resolved = packages.resolve()
        assert "intel-microcode" in resolved
        # Named for the architecture, not the vendor. "amd-microcode" is the
        # natural guess and exists in no Debian suite.
        assert "amd64-microcode" in resolved
        assert "amd-microcode" not in resolved

    def test_ships_every_xorg_video_driver(self):
        # The graphics-stack equivalent of MODULES=most.
        assert "xserver-xorg-video-all" in packages.resolve()

    def test_ships_wifi_firmware_for_the_common_chipsets(self):
        resolved = packages.resolve()
        for blob in ("firmware-iwlwifi", "firmware-realtek", "firmware-atheros", "firmware-brcm80211"):
            assert blob in resolved

    def test_uses_the_grub_bin_packages_not_the_debconf_ones(self):
        # grub-pc and grub-efi-amd64 run debconf and decide for themselves where
        # to install, which is precisely the decision portlin must make itself.
        resolved = packages.resolve(["boot"])
        assert "grub-pc-bin" in resolved
        assert "grub-efi-amd64-bin" in resolved
        assert "grub-pc" not in resolved
        assert "grub-efi-amd64" not in resolved

    def test_a_minimal_build_can_still_unlock_an_encrypted_root(self):
        resolved = packages.resolve(packages.MINIMAL_GROUPS)
        assert "cryptsetup" in resolved
        assert "cryptsetup-initramfs" in resolved

    def test_a_minimal_build_can_still_reach_a_network(self):
        assert "network-manager" in packages.resolve(packages.MINIMAL_GROUPS)

    def test_the_first_boot_wizard_has_its_dependencies(self):
        # The wizard is a python3 script driving whiptail. Missing either leaves
        # the stick with no way to create an account.
        resolved = packages.resolve(packages.MINIMAL_GROUPS)
        assert "whiptail" in resolved
        assert "python3" in resolved

    def test_no_swap_package_because_flash_wears_out(self):
        assert "zram-tools" in packages.resolve()


class TestDesktopTheme:
    def test_ships_a_theme_with_both_gtk_and_window_manager_variants(self):
        # Xfce's built-in themes have no dark variant for xfwm4, so relying on
        # GTK's built-in Adwaita-dark alone leaves the title bars light.
        assert "greybird-gtk-theme" in packages.resolve()

    def test_the_theme_is_desktop_only(self):
        assert "greybird-gtk-theme" not in packages.resolve(packages.MINIMAL_GROUPS)
