from __future__ import annotations

import pytest

from portlin.config import (
    DEFAULT_COMPONENTS,
    DEFAULT_SUITE,
    BuildConfig,
    WriteConfig,
)
from portlin.errors import BuildError


class TestBuildConfig:
    def test_defaults_target_debian_stable_with_firmware(self, tmp_path):
        cfg = BuildConfig(output=tmp_path / "r.tar.zst")
        assert cfg.suite == DEFAULT_SUITE
        assert "non-free-firmware" in cfg.components

    def test_components_split_into_a_list(self, tmp_path):
        cfg = BuildConfig(output=tmp_path / "r.tar.zst")
        assert cfg.component_list == DEFAULT_COMPONENTS.split()

    def test_output_is_coerced_to_a_path(self):
        cfg = BuildConfig(output="rootfs.tar.zst")
        assert cfg.output.name == "rootfs.tar.zst"

    def test_an_empty_suite_is_rejected(self, tmp_path):
        with pytest.raises(BuildError, match="suite"):
            BuildConfig(output=tmp_path / "r.tar.zst", suite="  ")

    def test_a_mirror_that_is_not_a_url_is_rejected(self, tmp_path):
        with pytest.raises(BuildError, match="does not look like a URL"):
            BuildConfig(output=tmp_path / "r.tar.zst", mirror="deb.debian.org")

    def test_package_list_reflects_the_selected_groups(self, tmp_path):
        cfg = BuildConfig(output=tmp_path / "r.tar.zst", groups=["boot"])
        assert "linux-image-amd64" in cfg.package_list()
        assert "xfce4" not in cfg.package_list()

    def test_an_explicit_work_dir_is_kept_after_the_build(self, tmp_path):
        cfg = BuildConfig(output=tmp_path / "r.tar.zst", work_dir=tmp_path / "w")
        assert cfg.work_dir == tmp_path / "w"


class TestWriteConfig:
    def test_defaults_are_conservative(self, tmp_path):
        cfg = WriteConfig(target="/dev/sdz", rootfs=tmp_path / "r.tar.zst")
        assert cfg.encrypt is False
        # discard leaks which blocks are unused to anyone inspecting the
        # ciphertext, so it stays opt-in.
        assert cfg.discard is False
        assert cfg.force is False
        assert cfg.assume_yes is False

    def test_encryption_requires_a_passphrase(self, tmp_path):
        with pytest.raises(BuildError, match="no passphrase"):
            WriteConfig(target="/dev/sdz", rootfs=tmp_path / "r.tar.zst", encrypt=True)

    def test_a_passphrase_without_encryption_is_a_mistake_worth_catching(self, tmp_path):
        # Silently ignoring it would leave someone believing their stick is
        # encrypted when it is not.
        with pytest.raises(BuildError, match="not enabled"):
            WriteConfig(
                target="/dev/sdz", rootfs=tmp_path / "r.tar.zst", passphrase="secret"
            )

    def test_a_valid_encrypted_config_is_accepted(self, tmp_path):
        cfg = WriteConfig(
            target="/dev/sdz",
            rootfs=tmp_path / "r.tar.zst",
            encrypt=True,
            passphrase="a good passphrase",
        )
        assert cfg.encrypt is True
