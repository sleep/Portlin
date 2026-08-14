from __future__ import annotations

import pytest

from portlin.errors import TargetError
from portlin.layout import GIB
from portlin.runner import Runner
from portlin.target import DeviceTarget, ImageTarget, open_target


class TestOpenTarget:
    def test_a_non_device_path_becomes_an_image_target(self, tmp_path, runner):
        target = open_target(str(tmp_path / "stick.img"), runner, image_size=32 * GIB)
        assert isinstance(target, ImageTarget)

    def test_a_missing_dev_path_is_an_error_rather_than_a_new_file(self, runner):
        # Without this guard, a typo like /dev/sdb1x would silently create a
        # regular file named after a device and appear to succeed.
        with pytest.raises(TargetError, match="not an existing block device"):
            open_target("/dev/sdb1x", runner)

    def test_image_size_is_rejected_for_a_real_device(self, tmp_path, runner, monkeypatch):
        monkeypatch.setattr("portlin.target._is_block_device", lambda path: True)
        device = tmp_path / "fake-device"
        device.touch()
        with pytest.raises(TargetError, match="meaningless for a real block device"):
            open_target(str(device), runner, image_size=32 * GIB)

    def test_an_existing_block_device_becomes_a_device_target(self, tmp_path, runner, monkeypatch):
        monkeypatch.setattr("portlin.target._is_block_device", lambda path: True)
        device = tmp_path / "fake-device"
        device.touch()
        assert isinstance(open_target(str(device), runner), DeviceTarget)


class TestImageTarget:
    def test_attaches_and_detaches_a_loop_device(self, tmp_path, runner):
        with ImageTarget(tmp_path / "s.img", runner, size_bytes=32 * GIB) as target:
            assert target.device == "/dev/loop0"
        assert runner.rendered()[-1] == "losetup -d /dev/loop0"

    def test_partitions_use_the_p_separator_on_loop_devices(self, tmp_path, runner):
        with ImageTarget(tmp_path / "s.img", runner, size_bytes=32 * GIB) as target:
            assert target.partition(4) == "/dev/loop0p4"

    def test_device_is_unavailable_before_attaching(self, tmp_path, runner):
        target = ImageTarget(tmp_path / "s.img", runner, size_bytes=32 * GIB)
        with pytest.raises(TargetError, match="not attached"):
            _ = target.device

    def test_allocates_a_sparse_file_of_the_requested_size(self, tmp_path, monkeypatch):
        # Real allocation, stubbed losetup: the file operations are the thing
        # under test and they work anywhere, while losetup exists only on Linux.
        path = tmp_path / "s.img"
        runner = Runner()
        monkeypatch.setattr(runner, "output", lambda *a, **kw: "/dev/loop9")
        ImageTarget(path, runner, size_bytes=64 * 1024 * 1024).open()

        assert path.stat().st_size == 64 * 1024 * 1024
        # Sparse: a 64 MiB image must not actually consume 64 MiB, or writing a
        # 32 GiB stick image would need 32 GiB of free space up front.
        assert path.stat().st_blocks * 512 < path.stat().st_size

    def test_refuses_to_resize_an_existing_image(self, tmp_path, runner):
        path = tmp_path / "s.img"
        path.write_bytes(b"\0" * 1024)
        target = ImageTarget(path, runner, size_bytes=32 * GIB)
        with pytest.raises(TargetError, match="different size"):
            target.open()

    def test_a_new_image_without_a_size_gets_the_small_default(self, tmp_path, runner):
        # The image is meant to be small; first-boot expansion claims the rest of
        # whatever stick it is written to, so there is no reason to make the
        # caller pick a number.
        from portlin.layout import DEFAULT_IMAGE_BYTES

        target = ImageTarget(tmp_path / "absent.img", runner)
        target.open()
        assert ["allocate", str(tmp_path / "absent.img"), str(DEFAULT_IMAGE_BYTES)] \
            in runner.commands

    def test_detaching_twice_is_harmless(self, tmp_path, runner):
        target = ImageTarget(tmp_path / "s.img", runner, size_bytes=32 * GIB)
        target.open()
        target.close()
        target.close()
        assert runner.rendered().count("losetup -d /dev/loop0") == 1


class TestDeviceTarget:
    def test_reads_its_size_from_blockdev(self, runner):
        assert DeviceTarget("/dev/sdb", runner).size_bytes() == 32 * GIB

    def test_partitions_have_no_separator_on_sd_devices(self, runner):
        assert DeviceTarget("/dev/sdb", runner).partition(2) == "/dev/sdb2"

    def test_an_unparseable_size_is_an_error(self, runner, monkeypatch):
        monkeypatch.setattr(runner, "output", lambda *a, **kw: "not a number")
        with pytest.raises(TargetError, match="could not determine the size"):
            DeviceTarget("/dev/sdb", runner).size_bytes()
