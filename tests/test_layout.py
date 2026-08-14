from __future__ import annotations

import pytest

from portlin.errors import LayoutError
from portlin.layout import (
    BOOT_MIB,
    ESP_MIB,
    GIB,
    MIB,
    MIN_ROOT_MIB,
    ROLE_BIOS,
    ROLE_BOOT,
    ROLE_ESP,
    ROLE_ROOT,
    format_size,
    partition_path,
    plan_partitions,
    sgdisk_argv,
)


class TestPlanPartitions:
    def test_produces_the_four_expected_roles_in_order(self):
        plan = plan_partitions(32 * GIB)
        assert [p.role for p in plan.partitions] == [
            ROLE_BIOS,
            ROLE_ESP,
            ROLE_BOOT,
            ROLE_ROOT,
        ]
        assert [p.number for p in plan.partitions] == [1, 2, 3, 4]

    def test_root_absorbs_the_remaining_space(self):
        plan = plan_partitions(32 * GIB)
        assert plan.by_role(ROLE_ROOT).size_mib is None
        # 32 GiB minus GPT slack, BIOS boot, ESP and /boot.
        assert plan.root_mib == (32 * 1024) - 2 - 1 - ESP_MIB - BOOT_MIB

    def test_encryption_costs_the_luks_header(self):
        plain = plan_partitions(32 * GIB, encrypted=False)
        encrypted = plan_partitions(32 * GIB, encrypted=True)
        assert plain.root_mib - encrypted.root_mib == 16
        assert encrypted.encrypted is True

    def test_rejects_a_target_too_small_for_a_usable_root(self):
        too_small = (MIN_ROOT_MIB + ESP_MIB + BOOT_MIB) * MIB
        with pytest.raises(LayoutError, match="too small"):
            plan_partitions(too_small)

    def test_error_names_the_actual_shortfall(self):
        with pytest.raises(LayoutError) as exc:
            plan_partitions(4 * GIB)
        assert "4096 MiB" in str(exc.value)
        assert str(MIN_ROOT_MIB) in str(exc.value)

    def test_rejects_a_zero_sized_target(self):
        with pytest.raises(LayoutError, match="zero bytes"):
            plan_partitions(0)

    def test_a_target_exactly_at_the_boundary_is_accepted(self):
        overhead = 2 + 1 + ESP_MIB + BOOT_MIB
        exact = (MIN_ROOT_MIB + overhead) * MIB
        assert plan_partitions(exact).root_mib == MIN_ROOT_MIB


class TestPartitionPath:
    @pytest.mark.parametrize(
        "device,number,expected",
        [
            ("/dev/sdb", 1, "/dev/sdb1"),
            ("/dev/sdb", 4, "/dev/sdb4"),
            ("/dev/sda", 12, "/dev/sda12"),
            # A trailing digit means the kernel inserts a 'p' separator, without
            # which we would silently address a completely different device.
            ("/dev/nvme0n1", 3, "/dev/nvme0n1p3"),
            ("/dev/loop0", 2, "/dev/loop0p2"),
            ("/dev/mmcblk0", 1, "/dev/mmcblk0p1"),
        ],
    )
    def test_naming_matches_kernel_convention(self, device, number, expected):
        assert partition_path(device, number) == expected

    def test_tolerates_a_trailing_slash(self):
        assert partition_path("/dev/sdb/", 1) == "/dev/sdb1"


class TestSgdiskArgv:
    def test_wipes_before_creating(self):
        commands = sgdisk_argv("/dev/sdb", plan_partitions(32 * GIB))
        assert commands[0] == ["sgdisk", "--zap-all", "/dev/sdb"]

    def test_creates_every_partition_with_type_and_name(self):
        commands = sgdisk_argv("/dev/sdb", plan_partitions(32 * GIB))
        create = commands[1]
        assert "-n1:0:+1M" in create
        assert "-t1:EF02" in create
        assert "-n2:0:+512M" in create
        assert "-t2:EF00" in create
        assert "-n3:0:+1024M" in create
        # The root partition takes everything left, expressed as size 0.
        assert "-n4:0:0" in create
        assert create[-1] == "/dev/sdb"

    def test_sets_the_legacy_bios_bootable_attribute(self):
        commands = sgdisk_argv("/dev/sdb", plan_partitions(32 * GIB))
        assert commands[-1] == ["sgdisk", "-A1:set:2", "/dev/sdb"]


class TestFormatSize:
    @pytest.mark.parametrize(
        "size,expected",
        [
            (512, "512 B"),
            (2048, "2 KiB"),
            (32 * GIB, "32.0 GiB"),
            (int(1.5 * GIB), "1.5 GiB"),
        ],
    )
    def test_renders_readable_sizes(self, size, expected):
        assert format_size(size) == expected
