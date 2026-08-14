from __future__ import annotations

import json

import pytest

from portlin.devices import BlockDevice, parse_lsblk, safety_problems
from portlin.errors import TargetError
from portlin.layout import GIB


def lsblk(*nodes) -> str:
    return json.dumps({"blockdevices": list(nodes)})


def disk(**overrides) -> dict:
    node = {
        "name": "sdb",
        "path": "/dev/sdb",
        "model": "SanDisk Ultra",
        "size": 32 * GIB,
        "rm": True,
        "hotplug": True,
        "type": "disk",
        "tran": "usb",
        "mountpoints": [None],
        "children": [],
    }
    node.update(overrides)
    return node


class TestParseLsblk:
    def test_reads_a_usb_stick(self):
        [device] = parse_lsblk(lsblk(disk()))
        assert device.path == "/dev/sdb"
        assert device.size_bytes == 32 * GIB
        assert device.removable is True
        assert device.transport == "usb"
        assert device.is_mounted is False

    def test_ignores_anything_that_is_not_a_whole_disk(self):
        nodes = [
            disk(),
            disk(name="sdb1", path="/dev/sdb1", type="part"),
            disk(name="loop0", path="/dev/loop0", type="loop"),
            disk(name="vg-root", path="/dev/mapper/vg-root", type="lvm"),
        ]
        assert [d.path for d in parse_lsblk(lsblk(*nodes))] == ["/dev/sdb"]

    def test_hotplug_alone_counts_as_removable(self):
        # Plenty of USB enclosures and card readers report rm=false while still
        # being perfectly removable, so treating rm as authoritative would make
        # portlin demand --force for ordinary sticks.
        [device] = parse_lsblk(lsblk(disk(rm=False, hotplug=True)))
        assert device.removable is True

    def test_an_internal_disk_is_not_removable(self):
        [device] = parse_lsblk(lsblk(disk(rm=False, hotplug=False, tran="sata")))
        assert device.removable is False

    def test_collects_mountpoints_from_partitions(self):
        node = disk(
            children=[
                {"name": "sdb1", "mountpoints": ["/media/photos"], "children": []},
                {"name": "sdb2", "mountpoints": [None], "children": []},
            ]
        )
        [device] = parse_lsblk(lsblk(node))
        assert device.mountpoints == ("/media/photos",)
        assert device.is_mounted is True

    def test_finds_mountpoints_nested_below_a_partition(self):
        node = disk(
            children=[
                {
                    "name": "sdb1",
                    "mountpoints": [None],
                    "children": [{"name": "crypt", "mountpoints": ["/mnt/x"], "children": []}],
                }
            ]
        )
        [device] = parse_lsblk(lsblk(node))
        assert device.mountpoints == ("/mnt/x",)

    def test_rejects_unparseable_output(self):
        with pytest.raises(TargetError, match="could not parse"):
            parse_lsblk("not json at all")

    def test_handles_a_machine_with_no_disks(self):
        assert parse_lsblk(lsblk()) == []


def device(**overrides) -> BlockDevice:
    values = {
        "path": "/dev/sdb",
        "name": "sdb",
        "model": "SanDisk Ultra",
        "size_bytes": 32 * GIB,
        "removable": True,
        "transport": "usb",
        "mountpoints": (),
    }
    values.update(overrides)
    return BlockDevice(**values)


class TestSafetyProblems:
    def test_a_healthy_usb_stick_has_no_problems(self):
        assert safety_problems(device()) == []

    def test_an_internal_disk_is_refused(self):
        problems = safety_problems(device(removable=False))
        assert len(problems) == 1
        assert "not removable" in problems[0]

    def test_force_waives_only_the_removability_check(self):
        assert safety_problems(device(removable=False), force=True) == []

    def test_force_does_not_waive_a_mounted_filesystem(self):
        # Writing under a mounted filesystem corrupts it no matter how sure the
        # user is, so this one is never waivable.
        problems = safety_problems(device(mountpoints=("/",)), force=True)
        assert any("mounted" in p for p in problems)

    def test_force_does_not_waive_a_too_small_device(self):
        # A stick too small to hold the image is useless however sure the user
        # is, so this is never waivable.
        problems = safety_problems(device(size_bytes=4 * GIB), force=True)
        assert any("minimum" in p for p in problems)

    def test_a_device_at_exactly_the_minimum_is_allowed(self):
        assert safety_problems(device(size_bytes=8 * GIB)) == []

    def test_reports_every_problem_at_once(self):
        problems = safety_problems(
            device(removable=False, size_bytes=2 * GIB, mountpoints=("/home",))
        )
        assert len(problems) == 3

    def test_describe_surfaces_what_the_user_needs_to_recognise_the_stick(self):
        text = device().describe()
        assert "/dev/sdb" in text
        assert "32.0 GiB" in text
        assert "SanDisk Ultra" in text
        assert "usb" in text
