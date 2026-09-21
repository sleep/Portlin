"""The pure half of portlin-migrate: what it reads, what it plans, never what it runs.

Everything here runs with no root, no Linux and no rsync. The planners produce
ordered argument lists, and those lists are the artefact under test, because
that is where a migration goes wrong in a way nothing else notices: a backup
directory named relative to the wrong root, a chown on a system path, a
source directory copied without its trailing slash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def migrate():
    return load_tool("migrate.py")


LSBLK = json.dumps({
    "blockdevices": [
        {"path": "/dev/sda", "label": None, "fstype": None, "size": "119.2G",
         "model": "Running Stick", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sda3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sda4", "label": "portlin-root", "fstype": "ext4", "size": "110G", "partn": 4},
         ]},
        {"path": "/dev/sdb", "label": None, "fstype": None, "size": "57.3G",
         "model": "SanDisk Ultra", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdb1", "label": None, "fstype": None, "size": "1M", "partn": 1},
             {"path": "/dev/sdb2", "label": "PORTLIN-ESP", "fstype": "vfat", "size": "512M", "partn": 2},
             {"path": "/dev/sdb3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sdb4", "label": None, "fstype": "crypto_LUKS", "size": "55.8G", "partn": 4},
         ]},
        {"path": "/dev/sdc", "label": None, "fstype": None, "size": "28.9G",
         "model": "Kingston", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdc3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/sdc4", "label": "portlin-root", "fstype": "ext4", "size": "27G", "partn": 4},
         ]},
        {"path": "/dev/sdd", "label": None, "fstype": None, "size": "1.8T",
         "model": "WD Elements", "tran": "usb", "partn": None,
         "children": [
             {"path": "/dev/sdd1", "label": "Backups", "fstype": "exfat", "size": "1.8T", "partn": 1},
         ]},
        {"path": "/dev/nvme0n1", "label": None, "fstype": None, "size": "931G",
         "model": "Samsung SSD", "tran": "nvme", "partn": None,
         "children": [
             {"path": "/dev/nvme0n1p3", "label": "portlin-boot", "fstype": "ext4", "size": "1G", "partn": 3},
             {"path": "/dev/nvme0n1p4", "label": None, "fstype": "ext4", "size": "900G", "partn": 4},
         ]},
    ]
})


class TestCandidates:
    def test_a_stick_is_recognised_by_its_boot_label_and_root_partition(self, migrate):
        found = migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        assert [c.path for c in found] == ["/dev/sdb4", "/dev/sdc4"]

    def test_an_encrypted_root_is_flagged_and_a_plain_one_is_not(self, migrate):
        by_path = {c.path: c for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")}
        assert by_path["/dev/sdb4"].encrypted is True
        assert by_path["/dev/sdc4"].encrypted is False

    def test_the_running_disk_is_never_offered(self, migrate):
        assert all(
            not c.path.startswith("/dev/sda")
            for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        )

    def test_a_root_without_the_root_label_is_not_a_stick(self, migrate):
        # nvme0n1 has a portlin-boot label but its fourth partition is plain
        # ext4 with no portlin-root label: somebody's own layout, not ours.
        assert "/dev/nvme0n1p4" not in [
            c.path for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda")
        ]

    def test_model_and_size_come_from_the_disk_not_the_partition(self, migrate):
        sdb = next(c for c in migrate.parse_lsblk(LSBLK, running_disk="/dev/sda") if c.path == "/dev/sdb4")
        assert sdb.model == "SanDisk Ultra"
        assert sdb.size == "57.3G"
        assert sdb.kind == "stick"

    def test_empty_or_broken_lsblk_output_means_no_candidates(self, migrate):
        assert migrate.parse_lsblk("", running_disk="/dev/sda") == []
        assert migrate.parse_lsblk("not json", running_disk="/dev/sda") == []

    def test_lsblk_is_asked_for_json_with_the_columns_the_parser_reads(self, migrate):
        argv = migrate.lsblk_argv()
        assert argv[:2] == ["lsblk", "--json"]
        assert "-o" in argv and argv[argv.index("-o") + 1] == migrate.LSBLK_COLUMNS

    def test_archives_are_found_directly_under_each_mount(self, migrate, tmp_path):
        media = tmp_path / "media" / "somebody" / "MyDrive"
        media.mkdir(parents=True)
        wanted = media / "office-2026-09-14.portlin-backup.tar.zst"
        wanted.write_bytes(b"")
        (media / "notes.txt").write_text("no")
        deeper = media / "old" / "x.portlin-backup.tar.zst"
        deeper.parent.mkdir()
        deeper.write_bytes(b"")
        found = migrate.archive_candidates([media])
        assert [c.path for c in found] == [str(wanted)]
        assert found[0].kind == "archive"
        assert found[0].model == "MyDrive"

    def test_describe_names_the_drive_and_whether_it_is_encrypted(self, migrate):
        stick = migrate.Candidate("stick", "/dev/sdb4", "SanDisk Ultra", "57.3G", True, "0.1.2")
        assert migrate.describe(stick) == "SanDisk Ultra 57.3G   portlin 0.1.2, encrypted"
        plain = migrate.Candidate("stick", "/dev/sdc4", "Kingston", "28.9G", False, "")
        assert migrate.describe(plain) == "Kingston 28.9G   portlin"
        archive = migrate.Candidate("archive", "/media/x/MyDrive/a.portlin-backup.tar.zst", "MyDrive")
        assert migrate.describe(archive) == "backup on MyDrive   a.portlin-backup.tar.zst"


class TestHuman:
    @pytest.mark.parametrize("size,text", [
        (0, "0 B"),
        (999, "999 B"),
        (3_000, "3 KB"),
        (512_000_000, "512 MB"),
        (1_483_920, "1.5 MB"),
        (14_200_000_000, "14.2 GB"),
        (2_500_000_000_000, "2.5 TB"),
    ])
    def test_decimal_units_like_portlin_info(self, migrate, size, text):
        assert migrate.human(size) == text


class TestRelease:
    def test_reads_the_version_out_of_portlin_release(self, migrate, tmp_path):
        (tmp_path / "etc").mkdir()
        (tmp_path / "etc" / "portlin-release").write_text(
            "PORTLIN_VERSION=0.1.2\nPORTLIN_URL=https://github.com/sleep/Portlin\n"
        )
        assert migrate.read_release(tmp_path)["PORTLIN_VERSION"] == "0.1.2"

    def test_a_root_without_the_breadcrumb_is_not_portlin(self, migrate, tmp_path):
        assert migrate.read_release(tmp_path) == {}
