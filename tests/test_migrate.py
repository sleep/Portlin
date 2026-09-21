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

    def test_a_partition_number_printed_as_a_string_still_matches(self, migrate):
        # PARTN can come back as either a JSON string or a number; the parser
        # should not depend on which, so a fixture that renders every partn
        # as a string has to match the same two candidates as the original.
        data = json.loads(LSBLK)
        for disk in data["blockdevices"]:
            for child in disk.get("children", []):
                if child["partn"] is not None:
                    child["partn"] = str(child["partn"])
        stringy = json.dumps(data)
        found = migrate.parse_lsblk(stringy, running_disk="/dev/sda")
        assert [c.path for c in found] == ["/dev/sdb4", "/dev/sdc4"]

    def test_blkid_export_lines_become_a_dict(self, migrate):
        parsed = migrate.parse_blkid_export("DEVNAME=/dev/sdb4\nLABEL=portlin-root\nTYPE=ext4\n")
        assert parsed == {"DEVNAME": "/dev/sdb4", "LABEL": "portlin-root", "TYPE": "ext4"}

    def test_a_blank_label_and_fstype_are_filled_in_by_probing(self, migrate):
        # lsblk reads label and fstype from udev, which nothing populates in
        # a container: only the two real sticks' partitions come back blank
        # here, the way they would with the udev database empty, while every
        # other device on the fixture keeps what lsblk already told us.
        data = json.loads(LSBLK)
        blanked_paths = {"/dev/sdb3", "/dev/sdb4", "/dev/sdc3", "/dev/sdc4"}
        originals = {}
        for disk in data["blockdevices"]:
            for child in disk.get("children", []):
                originals[child["path"]] = {"LABEL": child["label"], "TYPE": child["fstype"]}
                if child["path"] in blanked_paths:
                    child["label"] = None
                    child["fstype"] = None
        blanked = json.dumps(data)

        probed_paths = []

        def probe(path: str) -> dict[str, str]:
            probed_paths.append(path)
            return {k: v for k, v in originals[path].items() if v is not None}

        found = migrate.parse_lsblk(blanked, running_disk="/dev/sda", probe=probe)
        assert [c.path for c in found] == ["/dev/sdb4", "/dev/sdc4"]
        # Every blanked partition was asked about, and a device lsblk already
        # answered fully for (sdd1, label and fstype both present) never was:
        # _probed's early return means blkid is only the fallback, not a
        # second opinion asked of everything.
        assert blanked_paths <= set(probed_paths)
        assert "/dev/sdd1" not in probed_paths

    def test_with_no_probe_a_blank_label_means_no_candidates(self, migrate):
        data = json.loads(LSBLK)
        for disk in data["blockdevices"]:
            for child in disk.get("children", []):
                child["label"] = None
                child["fstype"] = None
        blanked = json.dumps(data)
        assert migrate.parse_lsblk(blanked, running_disk="/dev/sda") == []

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


def make_source(root: Path, *, user: str = "olduser", uid: int = 1500, nopasswd: bool = True,
                autologin: bool = False) -> Path:
    """A portlin root as a stick leaves it: one account, its home, its answers."""
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc" / "portlin-release").write_text("PORTLIN_VERSION=0.1.2\n")
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
        f"{user}:x:{uid}:{uid}:Old User,,,:/home/{user}:/bin/bash\n"
    )
    (root / "etc" / "shadow").write_text(
        "root:*:19000:0:99999:7:::\n"
        f"{user}:$y$j9T$abc$def:19000:0:99999:7:::\n"
    )
    (root / "etc" / "group").write_text(
        "root:x:0:\n"
        f"sudo:x:27:{user}\n"
        f"audio:x:29:{user}\n"
        f"plugdev:x:46:{user}\n"
        "scanner:x:120:\n"
        f"{user}:x:{uid}:\n"
    )
    if nopasswd:
        (root / "etc" / "sudoers.d").mkdir(exist_ok=True)
        (root / "etc" / "sudoers.d" / "50-portlin-nopasswd").write_text(
            f"{user} ALL=(ALL) NOPASSWD: ALL\n"
        )
    if autologin:
        conf = root / "etc" / "lightdm" / "lightdm.conf.d" / "10-portlin.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(f"[Seat:*]\nautologin-user={user}\nautologin-user-timeout=0\n")
    (root / "etc" / "hostname").write_text("office\n")
    (root / "etc" / "default").mkdir(exist_ok=True)
    (root / "etc" / "default" / "locale").write_text('LANG="en_GB.UTF-8"\n')
    (root / "etc" / "default" / "keyboard").write_text(
        'XKBMODEL=pc105\nXKBLAYOUT="gb"\nXKBVARIANT=""\n'
    )
    (root / "etc" / "timezone").write_text("Europe/London\n")
    home = root / "home" / user
    home.mkdir(parents=True, exist_ok=True)
    return home


class TestReadAccount:
    def test_finds_the_one_real_account_and_its_hash(self, migrate, tmp_path):
        make_source(tmp_path)
        accounts = migrate.read_accounts(tmp_path)
        assert [a.name for a in accounts] == ["olduser"]
        account = accounts[0]
        assert account.uid == 1500 and account.gid == 1500
        assert account.gecos == "Old User,,,"
        assert account.shell == "/bin/bash"
        assert account.home == "home/olduser"
        assert account.password_hash == "$y$j9T$abc$def"

    def test_groups_are_the_ones_that_list_the_account(self, migrate, tmp_path):
        make_source(tmp_path)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.groups == ("sudo", "audio", "plugdev")

    def test_the_sudo_waiver_and_autologin_are_read_as_answers(self, migrate, tmp_path):
        make_source(tmp_path, nopasswd=True, autologin=True)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.sudo_nopasswd is True
        assert account.autologin is True

    def test_no_waiver_and_no_autologin_file_mean_no(self, migrate, tmp_path):
        make_source(tmp_path, nopasswd=False, autologin=False)
        account = migrate.read_accounts(tmp_path)[0]
        assert account.sudo_nopasswd is False
        assert account.autologin is False

    def test_an_unreadable_shadow_leaves_the_hash_empty_rather_than_failing(self, migrate, tmp_path):
        make_source(tmp_path)
        (tmp_path / "etc" / "shadow").unlink()
        assert migrate.read_accounts(tmp_path)[0].password_hash == ""

    def test_a_root_with_no_passwd_has_no_accounts(self, migrate, tmp_path):
        assert migrate.read_accounts(tmp_path) == []


class TestReadIdentity:
    def test_reads_every_setting_the_wizard_wrote(self, migrate, tmp_path):
        make_source(tmp_path)
        identity = migrate.read_identity(tmp_path)
        assert identity == migrate.Identity(
            hostname="office", locale="en_GB.UTF-8", keyboard="gb", timezone="Europe/London"
        )

    def test_missing_files_leave_fields_empty(self, migrate, tmp_path):
        assert migrate.read_identity(tmp_path) == migrate.Identity()

    def test_the_timezone_falls_back_to_the_localtime_link(self, migrate, tmp_path):
        make_source(tmp_path)
        (tmp_path / "etc" / "timezone").unlink()
        (tmp_path / "etc" / "localtime").symlink_to("/usr/share/zoneinfo/Europe/Prague")
        assert migrate.read_identity(tmp_path).timezone == "Europe/Prague"


def populate_home(home: Path) -> None:
    (home / "Documents").mkdir()
    (home / "Documents" / "notes.txt").write_text("x" * 1000)
    (home / "Downloads").mkdir()
    (home / "Downloads" / "big.iso").write_text("y" * 5000)
    (home / ".config").mkdir()
    (home / ".config" / "app.ini").write_text("z" * 100)
    (home / ".cache").mkdir()
    (home / ".cache" / "junk").write_text("w" * 300)
    (home / ".bashrc").write_text("# rc\n")
    (home / ".portlin-migrate-backup").mkdir()
    (home / ".portlin-migrate-backup" / "old").write_text("gone")
    (home / "link-to-docs").symlink_to("Documents")


class TestInventory:
    @pytest.fixture
    def source(self, tmp_path):
        home = make_source(tmp_path)
        populate_home(home)
        return tmp_path

    def test_home_entries_split_into_files_and_settings(self, migrate, source):
        inventory = migrate.build_inventory(source)
        by_id = {item.id: item for item in inventory.items}
        assert by_id["home.files.Documents"].category == "home.files"
        assert by_id["home.files.Downloads"].paths == ("home/olduser/Downloads",)
        assert by_id["home.settings..config"].category == "home.settings"
        assert by_id["home.settings..bashrc"].paths == ("home/olduser/.bashrc",)

    def test_sizes_are_the_tree_totals(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.files.Documents"].bytes == 1000
        assert by_id["home.files.Downloads"].bytes == 5000
        assert by_id["home.settings..config"].bytes == 100

    def test_a_symlink_counts_as_itself_not_its_target(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.files.link-to-docs"].bytes < 1000

    def test_cache_is_listed_but_unticked_and_the_backup_tree_is_not_listed(self, migrate, source):
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["home.settings..cache"].default is False
        assert "home.settings..portlin-migrate-backup" not in by_id

    def test_the_account_and_identity_become_items(self, migrate, source):
        inventory = migrate.build_inventory(source)
        by_id = {item.id: item for item in inventory.items}
        assert by_id["account.olduser"].value == "olduser"
        assert by_id["identity.hostname"].value == "office"
        assert by_id["identity.locale"].value == "en_GB.UTF-8"
        assert by_id["identity.keyboard"].value == "gb"
        assert by_id["identity.timezone"].value == "Europe/London"
        assert inventory.accounts[0].name == "olduser"
        assert inventory.identity.hostname == "office"
        assert inventory.version == "0.1.2"
        assert inventory.hostname == "office"
        assert inventory.home == "home/olduser"

    def test_network_and_extras_appear_only_when_present(self, migrate, source):
        assert not [i for i in migrate.build_inventory(source).items if i.category in ("network", "extras")]
        connections = source / "etc/NetworkManager/system-connections"
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        bluetooth = source / "var/lib/bluetooth/AA:BB"
        bluetooth.mkdir(parents=True)
        (bluetooth / "settings").write_text("x")
        (source / "etc/cups").mkdir(parents=True)
        (source / "etc/cups/printers.conf").write_text("p")
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["network"].paths == ("etc/NetworkManager/system-connections",)
        assert by_id["extras.bluetooth"].paths == ("var/lib/bluetooth",)
        # ppd/ does not exist here, so only the file that does is planned.
        assert by_id["extras.printers"].paths == ("etc/cups/printers.conf",)

    def test_theme_names_are_read_out_of_the_overlay(self, migrate, source):
        xsettings = source / migrate.XSETTINGS
        xsettings.parent.mkdir(parents=True)
        xsettings.write_text(
            '<channel name="xsettings">\n'
            '  <property name="ThemeName" type="string" value="Greybird-dark"/>\n'
            '  <property name="IconThemeName" type="string" value="Papirus"/>\n'
            "</channel>\n"
        )
        assert migrate.read_theme_names(source) == {"theme": "Greybird-dark", "icons": "Papirus"}
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert by_id["extras.theme"].value == '{"theme": "Greybird-dark", "icons": "Papirus"}'
        assert "Greybird-dark" in by_id["extras.theme"].label

    def test_software_comes_from_records_then_from_the_catalog_check(self, migrate, source):
        catalog = load_tool("catalog.py")
        state = source / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text('{"packages": ["mullvad-vpn"]}')
        # A driver entry recorded on the old machine must be listed but off.
        nvidia = next(e for e in catalog.ENTRIES if e.category == "Drivers")
        (state / f"{nvidia.id}.json").write_text("{}")
        # An entry with no record but a dpkg check that matches.
        by_dpkg = next(e for e in catalog.ENTRIES if e.check.kind == "dpkg" and e.category != "Drivers"
                       and e.id != "mullvad")
        dpkg_status = f"{by_dpkg.check.values[0]} installed\n"
        by_id = {
            item.id: item
            for item in migrate.build_inventory(source, dpkg_status=dpkg_status).items
        }
        assert by_id["software.mullvad"].default is True
        assert by_id[f"software.{nvidia.id}"].default is False
        assert by_id[f"software.{by_dpkg.id}"].value == by_dpkg.id
        assert by_id["software.mullvad"].category == "software"

    def test_software_path_checks_are_rooted_at_the_source_not_this_machine(self, migrate, source):
        catalog = load_tool("catalog.py")
        # An absolute path check: catalog.expand_home leaves these alone, so
        # without rooting at the source this would read this machine's /opt.
        absolute = next(
            e for e in catalog.ENTRIES if e.check.kind == "path" and e.check.values[0].startswith("/")
        )
        target = source / absolute.check.values[0].lstrip("/")
        target.parent.mkdir(parents=True)
        target.write_text("x")
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert f"software.{absolute.id}" in by_id

        # A ~/-relative path check: it must resolve under the source's home,
        # not this machine's.
        home_relative = next(
            e for e in catalog.ENTRIES if e.check.kind == "path" and e.check.values[0].startswith("~/")
        )
        home_target = source / "home/olduser" / home_relative.check.values[0][2:]
        home_target.parent.mkdir(parents=True)
        home_target.write_text("x")
        by_id = {item.id: item for item in migrate.build_inventory(source).items}
        assert f"software.{home_relative.id}" in by_id

    def test_first_boot_leaves_software_unticked_because_there_is_no_network(self, migrate, source):
        state = source / migrate.SOFTWARE_STATE
        state.mkdir(parents=True)
        (state / "mullvad.json").write_text("{}")
        by_id = {item.id: item for item in migrate.build_inventory(source, firstboot=True).items}
        assert by_id["software.mullvad"].default is False
        assert "network" in by_id["software.mullvad"].note

    def test_items_come_out_in_category_order(self, migrate, source):
        order = [category for category, _ in migrate.CATEGORIES]
        seen = [item.category for item in migrate.build_inventory(source).items]
        assert seen == sorted(seen, key=order.index)

    def test_json_round_trips(self, migrate, source):
        inventory = migrate.build_inventory(source)
        assert migrate.from_json(migrate.to_json(inventory)) == inventory


class TestSelection:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        populate_home(make_source(tmp_path))
        return migrate.build_inventory(tmp_path)

    def test_the_default_plan_is_everything_ticked(self, migrate, inventory):
        ids = migrate.choose_ids(inventory)
        assert "home.files.Documents" in ids
        assert "home.settings..cache" not in ids

    def test_only_narrows_by_id_or_category(self, migrate, inventory):
        assert migrate.choose_ids(inventory, only=["home.files"]) == [
            i.id for i in inventory.items if i.category == "home.files"
        ]
        assert migrate.choose_ids(inventory, only=["home.settings..cache"]) == ["home.settings..cache"]

    def test_skip_removes_by_id_or_category(self, migrate, inventory):
        ids = migrate.choose_ids(inventory, skip=["account", "home.files.Downloads"])
        assert "account.olduser" not in ids
        assert "home.files.Downloads" not in ids
        assert "home.files.Documents" in ids

    def test_selected_bytes_sums_only_what_was_chosen(self, migrate, inventory):
        assert migrate.selected_bytes(inventory, ["home.files.Documents", "identity.hostname"]) == 1000

    def test_selected_keeps_inventory_order(self, migrate, inventory):
        chosen = migrate.selected(inventory, ["home.files.Downloads", "account.olduser"])
        assert [i.id for i in chosen] == ["account.olduser", "home.files.Downloads"]


STAMP = "2026-09-21-101500"


class TestPaths:
    def test_target_path_moves_home_entries_into_the_local_home(self, migrate):
        assert migrate.target_path("home/olduser/Documents", "home/olduser", "home/alice") == "home/alice/Documents"
        assert migrate.target_path("home/olduser", "home/olduser", "home/alice") == "home/alice"
        assert migrate.target_path("etc/NetworkManager/system-connections", "home/olduser", "home/alice") == "etc/NetworkManager/system-connections"
        # A sibling that merely starts with the same letters is not inside the home.
        assert migrate.target_path("home/olduser2/x", "home/olduser", "home/alice") == "home/olduser2/x"

    def test_backup_dir_is_under_the_home_for_home_paths_and_var_backups_otherwise(self, migrate, tmp_path):
        target = migrate.Target(root=tmp_path, user="alice", uid=1000, gid=1000, home="home/alice")
        assert migrate.backup_dir(target, "home/alice/.config", STAMP) == (
            tmp_path / "home/alice" / migrate.BACKUP_DIRNAME / STAMP / ".config"
        )
        assert migrate.backup_dir(target, "etc/cups/ppd", STAMP) == (
            tmp_path / migrate.SYSTEM_BACKUP_ROOT / STAMP / "etc/cups/ppd"
        )


class TestRsync:
    def test_a_directory_is_copied_with_trailing_slashes_and_a_backup_dir(self, migrate, tmp_path):
        source = tmp_path / "src" / "Documents"
        source.mkdir(parents=True)
        argv = migrate.rsync_argv(source, tmp_path / "dst" / "Documents",
                                  backup_dir=tmp_path / "bak", chown=(1000, 1000))
        assert argv[:2] == ("rsync", "-a")
        assert "--backup" in argv
        assert f"--backup-dir={tmp_path / 'bak'}" in argv
        assert "--info=progress2" in argv and "--no-inc-recursive" in argv
        assert "--chown=1000:1000" in argv
        assert argv[-2:] == (f"{source}/", f"{tmp_path / 'dst' / 'Documents'}/")
        assert "--delete" not in argv

    def test_a_file_and_a_symlink_are_copied_as_themselves(self, migrate, tmp_path):
        rc = tmp_path / ".bashrc"
        rc.write_text("# rc\n")
        assert migrate.rsync_argv(rc, tmp_path / "out" / ".bashrc", backup_dir=tmp_path / "b", chown=None)[-2:] == (
            str(rc), str(tmp_path / "out" / ".bashrc")
        )
        (tmp_path / "real").mkdir()
        link = tmp_path / "link"
        link.symlink_to("real")
        argv = migrate.rsync_argv(link, tmp_path / "out" / "link", backup_dir=tmp_path / "b", chown=None)
        assert argv[-2:] == (str(link), str(tmp_path / "out" / "link"))
        assert not any(a.startswith("--chown") for a in argv)

    @pytest.mark.parametrize("line,percent", [
        ("      1,234,567  45%   12.34MB/s    0:00:01 (xfr#12, to-chk=34/56)", 45),
        ("  5,000  100%    4.77MB/s    0:00:00 (xfr#3, to-chk=0/4)", 100),
        ("sending incremental file list", None),
        ("rsync: [sender] read errors mapping \"/x\": Permission denied (13)", None),
    ])
    def test_progress_is_read_off_rsyncs_summary_line(self, migrate, line, percent):
        assert migrate.parse_rsync_progress(line) == percent

    def test_overall_percent_weights_the_running_step_by_its_bytes(self, migrate):
        assert migrate.overall_percent(done=0, weight=1000, step_percent=50, total=4000) == 12
        assert migrate.overall_percent(done=3000, weight=1000, step_percent=100, total=4000) == 100
        assert migrate.overall_percent(done=0, weight=0, step_percent=0, total=0) == 100


class TestPlanStick:
    @pytest.fixture
    def parts(self, migrate, tmp_path):
        source = tmp_path / "source"
        home = make_source(source)
        populate_home(home)
        connections = source / migrate.CONNECTIONS
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        inventory = migrate.build_inventory(source)
        target = migrate.Target(root=tmp_path / "target", user="alice", uid=1000, gid=1000, home="home/alice")
        return source, inventory, target

    def test_one_rsync_per_selected_path_in_inventory_order(self, migrate, parts):
        source, inventory, target = parts
        ids = ["network", "home.files.Documents", "home.settings..config"]
        steps = migrate.plan_stick(inventory, ids, source, target, STAMP)
        assert [s.argv[0] for s in steps] == ["rsync", "rsync", "rsync"]
        assert [s.text for s in steps] == [
            "Copying Documents", "Copying .config", "Copying Saved network connections",
        ]

    def test_home_items_land_in_the_local_home_owned_by_the_local_account(self, migrate, parts):
        source, inventory, target = parts
        step = migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)[0]
        assert step.argv[-2:] == (f"{source}/home/olduser/Documents/", f"{target.root}/home/alice/Documents/")
        assert "--chown=1000:1000" in step.argv
        assert f"--backup-dir={target.root}/home/alice/{migrate.BACKUP_DIRNAME}/{STAMP}/Documents" in step.argv
        assert step.mkdir == (f"{target.root}/home/alice",)
        assert step.progress == "rsync"
        assert step.weight == 1000
        assert step.tolerate == migrate.RSYNC_PARTIAL

    def test_system_items_keep_their_path_and_their_owner(self, migrate, parts):
        source, inventory, target = parts
        step = migrate.plan_stick(inventory, ["network"], source, target, STAMP)[0]
        assert step.argv[-1] == f"{target.root}/etc/NetworkManager/system-connections/"
        assert not any(a.startswith("--chown") for a in step.argv)
        assert f"--backup-dir={target.root}/{migrate.SYSTEM_BACKUP_ROOT}/{STAMP}/etc/NetworkManager/system-connections" in step.argv

    def test_items_without_paths_plan_no_rsync(self, migrate, parts):
        source, inventory, target = parts
        assert migrate.plan_stick(inventory, ["identity.hostname", "account.olduser"], source, target, STAMP) == []


class TestSpace:
    def test_shortfall_keeps_a_reserve_for_the_system(self, migrate):
        assert migrate.shortfall(needed=1000, free=1000 + migrate.SPACE_RESERVE) == 0
        assert migrate.shortfall(needed=1000, free=1000) == migrate.SPACE_RESERVE
        assert migrate.shortfall(needed=5_000_000_000, free=1_000_000_000) == 4_000_000_000 + migrate.SPACE_RESERVE

    def test_the_refusal_names_the_gap_and_portlin_expand_when_it_would_help(self, migrate):
        devices = load_tool("devices.py")
        text = migrate.no_space_message(4_000_000_000, unclaimed=20_000_000_000)
        assert "4 GB" in text
        assert devices.UNCLAIMED_ADVICE.format(gb=20.0) in text
        assert "portlin-expand" not in migrate.no_space_message(4_000_000_000, unclaimed=0)


class TestArchive:
    @pytest.fixture
    def inventory(self, migrate, tmp_path):
        source = tmp_path / "source"
        populate_home(make_source(source))
        return migrate.build_inventory(source)

    def test_the_manifest_carries_the_inventory_and_round_trips(self, migrate, inventory):
        text = migrate.manifest_text(inventory, STAMP)
        restored, meta = migrate.read_manifest(text)
        assert restored == inventory
        assert meta["exported"] == STAMP
        assert meta["bytes"] == sum(i.bytes for i in inventory.items)

    def test_the_archive_is_named_for_the_host_and_the_day(self, migrate):
        assert migrate.archive_name("office", STAMP) == "office-2026-09-21.portlin-backup.tar.zst"

    def test_export_takes_every_path_including_the_unticked_ones(self, migrate, inventory):
        members = migrate.export_members(inventory)
        assert "home/olduser/.cache" in members
        assert "home/olduser/Documents" in members
        assert all("/" in m for m in members)

    def test_tar_create_writes_the_manifest_first_from_its_own_directory(self, migrate, tmp_path):
        argv = migrate.tar_create_argv(tmp_path / "a.tar.zst", tmp_path / "m", Path("/"), ["home/x/Documents"])
        assert argv[:3] == ("tar", "-I", "zstd -T0 -3")
        assert "--checkpoint-action=echo" in argv
        first = argv.index("-C")
        assert argv[first:first + 5] == ("-C", str(tmp_path / "m"), migrate.MANIFEST, "-C", "/")
        assert argv[-1] == "home/x/Documents"

    def test_plan_export_writes_the_manifest_then_archives(self, migrate, inventory, tmp_path):
        steps = migrate.plan_export(inventory, Path("/"), tmp_path / "out.tar.zst", tmp_path / "m", STAMP)
        assert steps[0].write[0][0] == str(tmp_path / "m" / migrate.MANIFEST)
        assert steps[0].mkdir == (str(tmp_path / "m"),)
        assert steps[1].argv[0] == "tar" and steps[1].progress == "tar"
        assert steps[1].weight == sum(i.bytes for i in inventory.items)
        assert steps[1].tolerate == (1,)

    @pytest.mark.parametrize("line,records", [
        ("tar: Write checkpoint 2000", 2000),
        ("tar: Read checkpoint 4000", 4000),
        ("home/x/Documents/notes.txt", None),
    ])
    def test_checkpoints_are_read_as_the_build_reads_them(self, migrate, line, records):
        assert migrate.parse_tar_checkpoint(line) == records
        assert migrate.checkpoint_bytes(2000) == 2000 * migrate.TAR_RECORD_BYTES

    def test_reading_the_manifest_extracts_that_member_to_stdout(self, migrate):
        assert migrate.tar_manifest_argv(Path("/a.tar.zst")) == ("tar", "-I", "zstd", "-xOf", "/a.tar.zst", "manifest.json")
        assert migrate.tar_list_argv(Path("/a.tar.zst")) == ("tar", "-I", "zstd", "-tf", "/a.tar.zst")


LISTING = "\n".join([
    "manifest.json",
    "home/olduser/Documents/",
    "home/olduser/Documents/notes.txt",
    "home/olduser/Downloads/",
    "home/olduser/Downloads/big.iso",
    "home/olduser/.config/",
    "home/olduser/.config/app.ini",
    "home/olduser/.bashrc",
    "etc/NetworkManager/system-connections/",
    "etc/NetworkManager/system-connections/cafe.nmconnection",
    "",
])


class TestPlanArchive:
    def test_chosen_members_are_the_files_under_the_chosen_paths(self, migrate):
        members = migrate.chosen_members(LISTING, ["home/olduser/Documents", "home/olduser/.bashrc"])
        assert members == ["home/olduser/Documents/notes.txt", "home/olduser/.bashrc"]

    def test_collisions_are_the_members_that_already_exist_on_the_target(self, migrate, tmp_path):
        target = migrate.Target(root=tmp_path, user="alice", uid=1000, gid=1000, home="home/alice")
        (tmp_path / "home/alice/Documents").mkdir(parents=True)
        (tmp_path / "home/alice/Documents/notes.txt").write_text("mine")
        (tmp_path / "etc/NetworkManager/system-connections").mkdir(parents=True)
        (tmp_path / "etc/NetworkManager/system-connections/cafe.nmconnection").write_text("old")
        members = [
            "home/olduser/Documents/notes.txt", "home/olduser/.bashrc",
            "etc/NetworkManager/system-connections/cafe.nmconnection",
        ]
        moves = migrate.collisions(members, "home/olduser", target, STAMP)
        assert moves == [
            (str(tmp_path / "home/alice/Documents/notes.txt"),
             str(tmp_path / "home/alice" / migrate.BACKUP_DIRNAME / STAMP / "Documents/notes.txt")),
            (str(tmp_path / "etc/NetworkManager/system-connections/cafe.nmconnection"),
             str(tmp_path / migrate.SYSTEM_BACKUP_ROOT / STAMP / "etc/NetworkManager/system-connections/cafe.nmconnection")),
        ]

    def test_extract_renames_the_home_and_never_keeps_the_archives_owners(self, migrate):
        argv = migrate.tar_extract_argv(
            Path("/a.tar.zst"), Path("/"), ["home/olduser/Documents", "etc/NetworkManager/system-connections"],
            source_home="home/olduser", target_home="home/alice",
        )
        assert "--no-same-owner" in argv
        assert "--transform=s|^home/olduser/|home/alice/|" in argv
        assert argv[argv.index("-C") + 1] == "/"
        assert argv[-2:] == ("home/olduser/Documents", "etc/NetworkManager/system-connections")
        same = migrate.tar_extract_argv(Path("/a.tar.zst"), Path("/"), ["home/x/D"], source_home="home/x", target_home="home/x")
        assert not any(a.startswith("--transform") for a in same)

    def test_the_plan_moves_aside_then_extracts_then_chowns_each_home_item(self, migrate, tmp_path):
        inventory, _ = migrate.read_manifest(migrate.manifest_text(
            migrate.build_inventory(_archive_source(migrate, tmp_path / "source")), STAMP
        ))
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        (tmp_path / "t/home/alice/Documents").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents/notes.txt").write_text("mine")
        steps = migrate.plan_archive(
            inventory, ["home.files.Documents", "home.settings..bashrc", "network"],
            tmp_path / "a.tar.zst", LISTING, target, STAMP,
        )
        extract, chown_docs, chown_rc = steps
        assert extract.argv[0] == "tar" and extract.progress == "tar"
        assert extract.move_aside == (
            (str(tmp_path / "t/home/alice/Documents/notes.txt"),
             str(tmp_path / "t/home/alice" / migrate.BACKUP_DIRNAME / STAMP / "Documents/notes.txt")),
        )
        assert extract.weight == sum(i.bytes for i in migrate.selected(inventory, ["home.files.Documents", "home.settings..bashrc", "network"]))
        assert extract.argv[-3:] == ("home/olduser/Documents", "home/olduser/.bashrc", "etc/NetworkManager/system-connections")
        assert chown_docs.argv == ("chown", "-R", "-h", "1000:1000", str(tmp_path / "t/home/alice/Documents"))
        assert chown_rc.argv == ("chown", "-R", "-h", "1000:1000", str(tmp_path / "t/home/alice/.bashrc"))


def _archive_source(migrate, root: Path) -> Path:
    populate_home(make_source(root))
    connections = root / migrate.CONNECTIONS
    connections.mkdir(parents=True)
    (connections / "cafe.nmconnection").write_text("[wifi]\n")
    return root


class TestSafety:
    def test_tar_create_argv_puts_a_double_dash_before_members(self, migrate, tmp_path):
        argv = migrate.tar_create_argv(tmp_path / "a.tar.zst", tmp_path / "m", Path("/"), ["home/x/Documents"])
        assert argv[-2:] == ("--", "home/x/Documents")

    def test_tar_extract_argv_puts_a_double_dash_before_paths(self, migrate):
        argv = migrate.tar_extract_argv(
            Path("/a.tar.zst"), Path("/"), ["home/olduser/Documents", "etc/NetworkManager/system-connections"],
            source_home="home/olduser", target_home="home/alice",
        )
        dd_index = argv.index("--")
        assert argv[dd_index + 1:dd_index + 3] == ("home/olduser/Documents", "etc/NetworkManager/system-connections")

    def test_tar_extract_argv_rejects_sed_metacharacters_in_source_home(self, migrate):
        with pytest.raises(ValueError, match="unsafe home path"):
            migrate.tar_extract_argv(
                Path("/a.tar.zst"), Path("/"), ["home/x/D"],
                source_home="home/x|y", target_home="home/alice",
            )

    def test_tar_extract_argv_rejects_sed_metacharacters_in_target_home(self, migrate):
        with pytest.raises(ValueError, match="unsafe home path"):
            migrate.tar_extract_argv(
                Path("/a.tar.zst"), Path("/"), ["home/x/D"],
                source_home="home/olduser", target_home="home/alice&bob",
            )

    def test_unsafe_paths_identifies_options_absolute_paths_traversals_and_system_files(self, migrate):
        items = [
            migrate.Item("bad1", "category", "option", paths=("--checkpoint-action=exec=sh",)),
            migrate.Item("bad2", "category", "system", paths=("etc/sudoers",)),
            migrate.Item("bad3", "category", "traversal", paths=("home/olduser/../../etc/shadow",)),
            migrate.Item("bad4", "category", "absolute", paths=("/etc/hosts",)),
            migrate.Item("good1", "home.files", "docs", paths=("home/olduser/Documents",)),
            migrate.Item("good2", "network", "network", paths=(migrate.CONNECTIONS,)),
        ]
        inventory = migrate.Inventory(
            version="0.1.2", hostname="office", home="home/olduser",
            items=tuple(items),
        )
        bad = migrate.unsafe_paths(inventory, ["bad1", "bad2", "bad3", "bad4", "good1", "good2"])
        assert bad == [
            "--checkpoint-action=exec=sh", "etc/sudoers", "home/olduser/../../etc/shadow", "/etc/hosts",
        ]

    def test_plan_archive_raises_when_any_selected_path_is_unsafe(self, migrate, tmp_path):
        items = [
            migrate.Item("bad", "category", "bad", paths=("--option",)),
            migrate.Item("good", "home.files", "good", paths=("home/olduser/Documents",)),
        ]
        inventory = migrate.Inventory(
            version="0.1.2", hostname="office", home="home/olduser",
            items=tuple(items),
        )
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        with pytest.raises(ValueError, match="refusing to touch"):
            migrate.plan_archive(inventory, ["bad"], tmp_path / "a.tar.zst", LISTING, target, STAMP)

    def test_plan_archive_succeeds_when_only_safe_paths_are_selected(self, migrate, tmp_path):
        items = [
            migrate.Item("bad", "category", "bad", paths=("--option",)),
            migrate.Item("good", "home.files", "good", paths=("home/olduser/Documents", "home/olduser/.bashrc")),
        ]
        inventory = migrate.Inventory(
            version="0.1.2", hostname="office", home="home/olduser",
            items=tuple(items),
        )
        target = migrate.Target(root=tmp_path / "t", user="alice", uid=1000, gid=1000, home="home/alice")
        steps = migrate.plan_archive(inventory, ["good"], tmp_path / "a.tar.zst", LISTING, target, STAMP)
        assert len(steps) >= 1

    def test_plan_stick_raises_when_any_selected_path_is_unsafe(self, migrate, tmp_path):
        items = [
            migrate.Item("bad", "category", "bad", paths=("etc/sudoers",)),
            migrate.Item("good", "home.files", "good", paths=("home/olduser/Documents",)),
        ]
        inventory = migrate.Inventory(
            version="0.1.2", hostname="office", home="home/olduser",
            items=tuple(items),
        )
        source = tmp_path / "source"
        source.mkdir()
        target = migrate.Target(root=tmp_path / "target")
        with pytest.raises(ValueError, match="refusing to touch"):
            migrate.plan_stick(inventory, ["bad"], source, target, STAMP)

    def test_plan_stick_succeeds_when_only_safe_paths_are_selected(self, migrate, tmp_path):
        items = [
            migrate.Item("bad", "category", "bad", paths=("etc/sudoers",)),
            migrate.Item("good", "home.files", "good", paths=("home/olduser/Documents",)),
        ]
        inventory = migrate.Inventory(
            version="0.1.2", hostname="office", home="home/olduser",
            items=tuple(items),
        )
        source = tmp_path / "source"
        make_source(source)
        populate_home(source / "home/olduser")
        target = migrate.Target(root=tmp_path / "target", user="alice", uid=1000, gid=1000, home="home/alice")
        steps = migrate.plan_stick(inventory, ["good"], source, target, STAMP)
        assert len(steps) >= 1


class TestAccountSteps:
    def account(self, migrate, **overrides):
        base = dict(name="olduser", uid=1500, gid=1500, gecos="Old User,,,", shell="/bin/bash",
                    home="home/olduser", password_hash="$y$j9T$abc$def",
                    groups=("sudo", "audio", "scanner", "lpadmin"), sudo_nopasswd=True, autologin=False)
        return migrate.Account(**{**base, **overrides})

    def test_the_account_is_recreated_with_its_ids_and_the_groups_that_exist_here(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups={"sudo", "audio", "video"}, uid_free=True)
        assert steps[0].argv == ("groupadd", "-f", "-g", "1500", "olduser")
        assert steps[1].argv == (
            "useradd", "--create-home", "--shell", "/bin/bash", "--comment", "Old User,,,",
            "--groups", "sudo,audio", "--uid", "1500", "--gid", "1500", "olduser",
        )

    def test_the_password_moves_as_its_hash_on_stdin(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups={"sudo"}, uid_free=True)
        last = steps[-1]
        assert last.argv == ("chpasswd", "-e")
        assert last.stdin == "olduser:$y$j9T$abc$def\n"
        assert "$y$" not in last.text

    def test_a_taken_uid_lets_useradd_pick_and_says_so(self, migrate):
        steps = migrate.account_steps(self.account(migrate), existing_groups=set(), uid_free=False)
        assert steps[0].argv[0] == "useradd"
        assert "--uid" not in steps[0].argv and "--user-group" in steps[0].argv
        assert steps[0].warn is None
        assert any("1500" in (s.warn or "") for s in steps)

    def test_no_hash_means_no_password_step_and_a_warning(self, migrate):
        steps = migrate.account_steps(self.account(migrate, password_hash=""), existing_groups=set(), uid_free=True)
        assert not any(s.argv and s.argv[0] == "chpasswd" for s in steps)
        assert any("password" in (s.warn or "") for s in steps)


class TestIdentitySteps:
    def test_hostname_rewrites_hosts_and_asks_hostnamectl_optionally(self, migrate):
        hosts = "127.0.0.1\tlocalhost\n127.0.1.1\tportlin\n::1\tlocalhost ip6-localhost\n"
        steps = migrate.identity_steps(migrate.Identity(hostname="office"), ["identity.hostname"], hosts_text=hosts)
        step = steps[0]
        assert ("/etc/hostname", "office\n") in step.write
        assert ("/etc/hosts", "127.0.0.1\tlocalhost\n127.0.1.1\toffice\n::1\tlocalhost ip6-localhost\n") in step.write
        assert step.argv == ("hostnamectl", "set-hostname", "office")
        assert step.optional is True

    def test_locale_generates_then_records(self, migrate):
        steps = migrate.identity_steps(migrate.Identity(locale="en_GB.UTF-8"), ["identity.locale"], hosts_text="")
        gen, record = steps
        assert gen.write == (("/etc/locale.gen", "en_GB.UTF-8 UTF-8\nC.UTF-8 UTF-8\n"),)
        assert gen.argv == ("locale-gen",)
        assert record.write == (("/etc/default/locale", 'LANG="en_GB.UTF-8"\n'),)
        assert record.argv == ("localectl", "set-locale", "LANG=en_GB.UTF-8") and record.optional

    def test_keyboard_and_timezone(self, migrate):
        steps = migrate.identity_steps(
            migrate.Identity(keyboard="gb", timezone="Europe/London"),
            ["identity.keyboard", "identity.timezone"], hosts_text="",
        )
        keyboard, setupcon, link, tz = steps
        assert keyboard.write[0][0] == "/etc/default/keyboard"
        assert 'XKBLAYOUT="gb"' in keyboard.write[0][1]
        assert keyboard.argv == ("setupcon", "--save") and keyboard.optional
        assert setupcon.argv == ("localectl", "set-x11-keymap", "gb") and setupcon.optional
        assert link.write == (("/etc/timezone", "Europe/London\n"),)
        assert link.argv == ("ln", "-sf", "/usr/share/zoneinfo/Europe/London", "/etc/localtime")
        assert tz.argv == ("timedatectl", "set-timezone", "Europe/London") and tz.optional

    def test_only_the_chosen_settings_are_applied(self, migrate):
        identity = migrate.Identity(hostname="office", locale="en_GB.UTF-8", keyboard="gb", timezone="Europe/London")
        assert migrate.identity_steps(identity, ["identity.keyboard"], hosts_text="")[0].write[0][0] == "/etc/default/keyboard"
        assert migrate.identity_steps(identity, [], hosts_text="") == []


class TestThemeSteps:
    def test_targets_match_the_wizards(self, migrate):
        from test_firstboot import WIZARD, module_constant
        assert migrate.THEME_TARGETS == module_constant(WIZARD, "THEME_TARGETS")
        assert migrate.ICON_THEME_TARGETS == module_constant(WIZARD, "ICON_THEME_TARGETS")

    def test_rewrite_returns_none_when_the_file_does_not_take_the_name(self, migrate):
        pattern, replacement = migrate.THEME_TARGETS["/etc/xdg/xdg-portlin/gtk-3.0/settings.ini"]
        assert migrate.rewrite_theme("[Settings]\ngtk-theme-name=Numix\n", pattern, replacement, "Greybird-dark") == (
            "[Settings]\ngtk-theme-name=Greybird-dark\n"
        )
        assert migrate.rewrite_theme("[Settings]\n", pattern, replacement, "Greybird-dark") is None

    def test_every_target_file_is_rewritten_or_none_is(self, migrate, tmp_path):
        for path in {**migrate.THEME_TARGETS, **migrate.ICON_THEME_TARGETS}:
            file = tmp_path / path.lstrip("/")
            file.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml").write_text(
            '<property name="ThemeName" type="string" value="Numix"/>\n'
            '<property name="IconThemeName" type="string" value="Papirus-Dark"/>\n'
        )
        (tmp_path / "etc/xdg/xdg-portlin/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml").write_text(
            '<property name="theme" type="string" value="Numix"/>\n'
        )
        (tmp_path / "etc/xdg/xdg-portlin/gtk-3.0/settings.ini").write_text(
            "gtk-theme-name=Numix\ngtk-icon-theme-name=Papirus-Dark\n"
        )
        (tmp_path / "etc/xdg/xdg-portlin/gtk-4.0/settings.ini").write_text("gtk-icon-theme-name=Papirus-Dark\n")
        (tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf").write_text(
            "theme-name=Numix\nicon-theme-name=Papirus-Dark\n"
        )
        target = migrate.Target(root=tmp_path)
        steps = migrate.theme_steps({"theme": "Greybird-dark", "icons": "Papirus"}, target, icon_theme_installed=True)
        written = {path: text for step in steps for path, text in step.write}
        assert written[str(tmp_path / "etc/xdg/xdg-portlin/gtk-3.0/settings.ini")] == (
            "gtk-theme-name=Greybird-dark\ngtk-icon-theme-name=Papirus\n"
        )
        assert "Greybird-dark" in written[str(tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf")]
        assert "Papirus\n" in written[str(tmp_path / "etc/xdg/xdg-portlin/gtk-4.0/settings.ini")]
        # A greeter file that does not take the name means the widget theme is
        # not applied anywhere: three of four is worse than none.
        (tmp_path / "etc/lightdm/lightdm-gtk-greeter.conf.d/50-portlin.conf").write_text("nothing\n")
        steps = migrate.theme_steps({"theme": "Greybird-dark"}, target, icon_theme_installed=True)
        assert [s.write for s in steps] == [()]
        assert "Greybird-dark" in steps[0].warn

    def test_an_icon_theme_not_installed_here_is_left_alone(self, migrate, tmp_path):
        steps = migrate.theme_steps({"icons": "Numix-Circle"}, migrate.Target(root=tmp_path), icon_theme_installed=False)
        assert [s.write for s in steps] == [()]
        assert "Numix-Circle" in steps[0].warn


class TestSoftwareSteps:
    def test_each_entry_is_installed_through_the_installer_and_may_fail(self, migrate):
        steps = migrate.software_steps(["mullvad", "vlc"])
        assert [s.argv for s in steps] == [
            (migrate.INSTALLER, "install", "mullvad"),
            (migrate.INSTALLER, "install", "vlc"),
        ]
        assert all(s.passthrough and s.optional for s in steps)


class TestExistingAccount:
    def test_after_setup_the_account_item_is_off_and_says_why(self, migrate, tmp_path):
        inventory = migrate.build_inventory(make_source(tmp_path).parent.parent)
        marked = migrate.mark_existing_account(inventory, "alice")
        item = next(i for i in marked.items if i.category == "account")
        assert item.default is False
        assert "alice" in item.note and "already" in item.note
        assert migrate.mark_existing_account(inventory, "") == inventory


class TestDpkgStatus:
    def test_the_status_file_becomes_the_lines_the_catalog_check_reads(self, migrate):
        text = (
            "Package: vlc\nStatus: install ok installed\nVersion: 3\n\n"
            "Package: gone\nStatus: deinstall ok config-files\n\n"
            "Package: half\nStatus: install ok unpacked\n\n"
        )
        assert migrate.dpkg_status_lines(text) == "vlc installed\ngone config-files\nhalf unpacked\n"


class TestHostileSource:
    """Everything read off a source stick or out of an archive is somebody
    else's data, and the plan it feeds runs as root. Each case here is a
    value that would have reached a rename, a shell-sourced file or an argv
    unchecked; the boundary that refuses it is what these pin."""

    def _inventory(self, migrate, items):
        return migrate.Inventory(version="0.1.2", hostname="office", home="home/olduser", items=tuple(items))

    def _target(self, migrate, root: Path):
        return migrate.Target(root=root, user="alice", uid=1000, gid=1000, home="home/alice")

    # C1: the tar listing is attacker-controlled and only needs to start
    # with a chosen path to be walked by collisions.
    def test_a_traversing_member_under_a_chosen_path_is_refused_not_dropped(self, migrate, tmp_path):
        listing = LISTING + "home/olduser/Documents/../../../etc/passwd\n"
        assert migrate.unsafe_members(listing, ["home/olduser/Documents"]) == [
            "home/olduser/Documents/../../../etc/passwd",
        ]
        assert "home/olduser/Documents/../../../etc/passwd" not in migrate.chosen_members(
            listing, ["home/olduser/Documents"]
        )
        items = [migrate.Item("docs", "home.files", "Documents", paths=("home/olduser/Documents",))]
        target = self._target(migrate, tmp_path / "t")
        with pytest.raises(ValueError, match="refusing to touch .*etc/passwd"):
            migrate.plan_archive(self._inventory(migrate, items), ["docs"], tmp_path / "a.tar.zst", listing, target, STAMP)

    def test_a_member_with_an_empty_part_is_refused_too(self, migrate):
        listing = "home/olduser/Documents//notes.txt\n"
        assert migrate.unsafe_members(listing, ["home/olduser/Documents"]) == ["home/olduser/Documents//notes.txt"]
        assert migrate.chosen_members(listing, ["home/olduser/Documents"]) == []

    def test_a_collision_reached_through_a_symlink_out_of_the_home_is_refused(self, migrate, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "notes.txt").write_text("not yours")
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents").symlink_to(outside)
        with pytest.raises(ValueError, match="notes.txt"):
            migrate.collisions(["home/olduser/Documents/notes.txt"], "home/olduser", target, STAMP)
        assert (outside / "notes.txt").exists()

    def test_a_system_collision_reached_through_a_symlink_is_refused(self, migrate, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "cafe.nmconnection").write_text("not yours")
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/etc/NetworkManager").mkdir(parents=True)
        (tmp_path / "t/etc/NetworkManager/system-connections").symlink_to(outside)
        with pytest.raises(ValueError, match="cafe.nmconnection"):
            migrate.collisions(["etc/NetworkManager/system-connections/cafe.nmconnection"], "home/olduser", target, STAMP)

    def test_a_symlink_inside_the_home_still_collides_normally(self, migrate, tmp_path):
        # A link that stays inside the home is the ordinary case, and the
        # link itself is what gets moved aside, never what it points at.
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice/Real").mkdir(parents=True)
        (tmp_path / "t/home/alice/Real/notes.txt").write_text("mine")
        (tmp_path / "t/home/alice/Documents").symlink_to(tmp_path / "t/home/alice/Real")
        (tmp_path / "t/home/alice/.bashrc").symlink_to("/etc/skel/.bashrc")
        moves = migrate.collisions(
            ["home/olduser/Documents/notes.txt", "home/olduser/.bashrc"], "home/olduser", target, STAMP,
        )
        assert [existing for existing, _ in moves] == [
            str(tmp_path / "t/home/alice/Documents/notes.txt"), str(tmp_path / "t/home/alice/.bashrc"),
        ]

    # I4: rsync follows a symlinked destination when the path ends in a slash.
    def test_plan_stick_refuses_a_destination_symlinked_out_of_the_home(self, migrate, tmp_path):
        source = tmp_path / "source"
        populate_home(make_source(source))
        inventory = migrate.build_inventory(source)
        outside = tmp_path / "outside"
        outside.mkdir()
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents").symlink_to(outside)
        with pytest.raises(ValueError, match="Documents"):
            migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)
        # The same link with nothing yet at the destination is fine, as is a
        # link that stays inside the home.
        (tmp_path / "t/home/alice/Documents").unlink()
        assert migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)
        (tmp_path / "t/home/alice/Real").mkdir()
        (tmp_path / "t/home/alice/Documents").symlink_to(tmp_path / "t/home/alice/Real")
        assert migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)

    def test_plan_stick_refuses_a_system_destination_symlinked_elsewhere(self, migrate, tmp_path):
        source = tmp_path / "source"
        make_source(source)
        connections = source / migrate.CONNECTIONS
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        inventory = migrate.build_inventory(source)
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/etc/NetworkManager").mkdir(parents=True)
        (tmp_path / "t/etc/NetworkManager/system-connections").symlink_to(tmp_path / "t/home/alice")
        with pytest.raises(ValueError, match="system-connections"):
            migrate.plan_stick(inventory, ["network"], source, target, STAMP)

    # C2: identity values land in shell-sourced files, /etc/hosts and a link.
    @pytest.mark.parametrize("field, value", [
        ("keyboard", 'us"; touch /tmp/pwned; echo "'),
        ("hostname", "evil\n1.2.3.4 bank.example"),
        ("hostname", "office\n"),
        ("hostname", "-rf"),
        ("timezone", "../../../etc/passwd"),
        ("timezone", "Europe/../../../etc/passwd"),
        ("locale", "en_GB.UTF-8\"; rm -rf /; echo \""),
    ])
    def test_an_invalid_identity_value_is_warned_about_and_not_written(self, migrate, field, value):
        identity = migrate.Identity(**{field: value})
        steps = migrate.identity_steps(identity, [f"identity.{field}"], hosts_text="127.0.0.1\tlocalhost\n")
        assert len(steps) == 1
        assert steps[0].warn == f"ignoring the source's {field} {value!r}: not a valid {field}"
        assert steps[0].write == () and steps[0].argv is None

    def test_ordinary_identity_values_still_pass(self, migrate):
        identity = migrate.Identity(hostname="my-laptop", locale="pt_BR.UTF-8", keyboard="de", timezone="America/Sao_Paulo")
        steps = migrate.identity_steps(
            identity, ["identity.hostname", "identity.locale", "identity.keyboard", "identity.timezone"], hosts_text="",
        )
        assert not any(s.warn for s in steps)

    def test_the_hostname_and_username_rules_are_the_wizards(self, migrate):
        from test_firstboot import WIZARD
        body = WIZARD.read_text()
        assert f'HOSTNAME_RE = re.compile(r"{migrate.HOSTNAME_RE.pattern}")' in body
        assert f'USERNAME_RE = re.compile(r"{migrate.USERNAME_RE.pattern}")' in body

    # I2: account fields go straight into useradd's argv and chpasswd's stdin.
    def _account(self, migrate, **overrides):
        base = dict(name="olduser", uid=1500, gid=1500, gecos="Old User,,,", shell="/bin/bash",
                    home="home/olduser", password_hash="$y$j9T$abc$def",
                    groups=("sudo",), sudo_nopasswd=True, autologin=False)
        return migrate.Account(**{**base, **overrides})

    @pytest.mark.parametrize("name", ["-r", "--root", "Evil", "a b", "x" * 33, "", "root:x", "olduser\n"])
    def test_a_bad_account_name_yields_one_warning_and_no_account(self, migrate, name):
        steps = migrate.account_steps(self._account(migrate, name=name), existing_groups={"sudo"}, uid_free=True)
        assert len(steps) == 1
        assert steps[0].argv is None and steps[0].warn and repr(name) in steps[0].warn

    def test_a_shell_not_in_the_local_shells_file_becomes_bash(self, migrate):
        steps = migrate.account_steps(
            self._account(migrate, shell="/tmp/evil"), existing_groups=set(), uid_free=True, shells={"/bin/bash", "/bin/zsh"},
        )
        useradd = next(s.argv for s in steps if s.argv and s.argv[0] == "useradd")
        assert useradd[useradd.index("--shell") + 1] == "/bin/bash"
        steps = migrate.account_steps(
            self._account(migrate, shell="/bin/zsh"), existing_groups=set(), uid_free=True, shells={"/bin/bash", "/bin/zsh"},
        )
        useradd = next(s.argv for s in steps if s.argv and s.argv[0] == "useradd")
        assert useradd[useradd.index("--shell") + 1] == "/bin/zsh"
        # No shells file to consult means only bash.
        steps = migrate.account_steps(self._account(migrate, shell="/bin/zsh"), existing_groups=set(), uid_free=True)
        useradd = next(s.argv for s in steps if s.argv and s.argv[0] == "useradd")
        assert useradd[useradd.index("--shell") + 1] == "/bin/bash"

    def test_newlines_are_stripped_from_the_gecos(self, migrate):
        steps = migrate.account_steps(
            self._account(migrate, gecos="Old\nUser\r,,,"), existing_groups=set(), uid_free=True,
        )
        useradd = next(s.argv for s in steps if s.argv and s.argv[0] == "useradd")
        comment = useradd[useradd.index("--comment") + 1]
        assert "\n" not in comment and "\r" not in comment and "Old" in comment and "User" in comment

    def test_a_hash_that_would_add_a_second_chpasswd_line_is_treated_as_unreadable(self, migrate):
        steps = migrate.account_steps(
            self._account(migrate, password_hash="$y$abc\nroot:$y$pwned"), existing_groups=set(), uid_free=True,
        )
        assert not any(s.argv and s.argv[0] == "chpasswd" for s in steps)
        assert any("password" in (s.warn or "") for s in steps)

    # I2: the theme name is spliced into a re.sub replacement template.
    def test_a_theme_name_with_replacement_syntax_is_inserted_literally(self, migrate):
        pattern, replacement = migrate.THEME_TARGETS["/etc/xdg/xdg-portlin/gtk-3.0/settings.ini"]
        text = "[Settings]\ngtk-theme-name=Numix\n"
        assert migrate.rewrite_theme(text, pattern, replacement, r"\g<0>") == "[Settings]\ngtk-theme-name=\\g<0>\n"
        assert migrate.rewrite_theme(text, pattern, replacement, r"a\1b") == "[Settings]\ngtk-theme-name=a\\1b\n"

    @pytest.mark.parametrize("name", [r"\g<0>", "Numix\nevil", 'x"/>', "a;b"])
    def test_a_theme_name_outside_the_allowed_characters_is_warned_about_and_skipped(self, migrate, tmp_path, name):
        target = migrate.Target(root=tmp_path)
        steps = migrate.theme_steps({"theme": name, "icons": name}, target, icon_theme_installed=True)
        assert all(s.write == () and s.argv is None and s.warn for s in steps)
        assert len(steps) == 2

    # T3: the hash must not leak through a logged or printed Account.
    def test_the_password_hash_is_kept_out_of_the_accounts_repr(self, migrate):
        assert "$y$" not in repr(self._account(migrate))

    # Round two: the system-path half of the destination check, the archive
    # planner's destinations, and the backup side of every rename.
    def test_plan_stick_refuses_a_system_destination_with_a_symlink_on_the_way(self, migrate, tmp_path):
        source = tmp_path / "source"
        make_source(source)
        (source / "etc/cups/ppd").mkdir(parents=True)
        (source / "etc/cups/ppd/printer.ppd").write_text("*PPD\n")
        inventory = migrate.build_inventory(source)
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/etc/cups").mkdir(parents=True)
        (tmp_path / "t/etc/sudoers.d").mkdir(parents=True)
        # Still under /etc, so a check that only asks for the /etc tree
        # would let rsync write into sudoers.d through it.
        (tmp_path / "t/etc/cups/ppd").symlink_to(tmp_path / "t/etc/sudoers.d")
        with pytest.raises(ValueError, match="etc/cups/ppd"):
            migrate.plan_stick(inventory, ["extras.printers"], source, target, STAMP)
        (tmp_path / "t/etc/cups/ppd").unlink()
        (tmp_path / "t/etc/cups/ppd").mkdir()
        assert migrate.plan_stick(inventory, ["extras.printers"], source, target, STAMP)

    def test_plan_archive_refuses_a_destination_symlinked_out_of_the_home(self, migrate, tmp_path):
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/etc/sudoers.d").mkdir(parents=True)
        (tmp_path / "t/home/alice").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents").symlink_to(tmp_path / "t/etc/sudoers.d")
        items = [migrate.Item("docs", "home.files", "Documents", paths=("home/olduser/Documents",))]
        # Nothing at the member's own name, so collisions alone has nothing
        # to refuse; tar would still write through the link.
        listing = "home/olduser/Documents/\nhome/olduser/Documents/evil\n"
        with pytest.raises(ValueError, match="Documents"):
            migrate.plan_archive(self._inventory(migrate, items), ["docs"], tmp_path / "a.tar.zst", listing, target, STAMP)

    def test_a_path_naming_the_backup_tree_is_unsafe(self, migrate):
        items = [
            migrate.Item("bad1", "home.settings", "backup", paths=(f"home/olduser/{migrate.BACKUP_DIRNAME}",)),
            migrate.Item("bad2", "home.files", "nested", paths=(f"home/olduser/Documents/{migrate.BACKUP_DIRNAME}/x",)),
            migrate.Item("good", "home.files", "docs", paths=("home/olduser/Documents",)),
        ]
        bad = migrate.unsafe_paths(self._inventory(migrate, items), ["bad1", "bad2", "good"])
        assert bad == [f"home/olduser/{migrate.BACKUP_DIRNAME}", f"home/olduser/Documents/{migrate.BACKUP_DIRNAME}/x"]
        listing = f"home/olduser/Documents/{migrate.BACKUP_DIRNAME}/x\n"
        assert migrate.unsafe_members(listing, ["home/olduser/Documents"]) == [
            f"home/olduser/Documents/{migrate.BACKUP_DIRNAME}/x",
        ]

    def test_plan_stick_refuses_a_backup_tree_symlinked_out_of_the_home(self, migrate, tmp_path):
        source = tmp_path / "source"
        populate_home(make_source(source))
        inventory = migrate.build_inventory(source)
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice").mkdir(parents=True)
        (tmp_path / "t/etc").mkdir()
        (tmp_path / "t/home/alice" / migrate.BACKUP_DIRNAME).symlink_to(tmp_path / "t/etc")
        with pytest.raises(ValueError, match=migrate.BACKUP_DIRNAME):
            migrate.plan_stick(inventory, ["home.files.Documents"], source, target, STAMP)

    def test_plan_stick_refuses_a_system_backup_tree_symlinked_elsewhere(self, migrate, tmp_path):
        source = tmp_path / "source"
        make_source(source)
        connections = source / migrate.CONNECTIONS
        connections.mkdir(parents=True)
        (connections / "cafe.nmconnection").write_text("[wifi]\n")
        inventory = migrate.build_inventory(source)
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/var/backups").mkdir(parents=True)
        (tmp_path / "t/home/alice").mkdir(parents=True)
        (tmp_path / "t/var/backups/portlin-migrate").symlink_to(tmp_path / "t/home/alice")
        with pytest.raises(ValueError, match="portlin-migrate"):
            migrate.plan_stick(inventory, ["network"], source, target, STAMP)

    def test_ordinary_links_in_the_home_still_copy_and_collide(self, migrate, tmp_path):
        source = tmp_path / "source"
        populate_home(make_source(source))
        inventory = migrate.build_inventory(source)
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice/Documents").mkdir(parents=True)
        (tmp_path / "t/home/alice/link-to-docs").symlink_to("Documents")
        (tmp_path / "t/home/alice/.bashrc").write_text("# mine\n")
        ids = ["home.files.link-to-docs", "home.settings..bashrc"]
        steps = migrate.plan_stick(inventory, ids, source, target, STAMP)
        assert [s.argv[-1] for s in steps] == [
            str(tmp_path / "t/home/alice/link-to-docs"), str(tmp_path / "t/home/alice/.bashrc"),
        ]
        members = ["home/olduser/link-to-docs", "home/olduser/.bashrc"]
        assert [e for e, _ in migrate.collisions(members, "home/olduser", target, STAMP)] == [
            str(tmp_path / "t/home/alice/link-to-docs"), str(tmp_path / "t/home/alice/.bashrc"),
        ]

    @pytest.mark.parametrize("value", ["/Europe/London", "Europe/London/", "localtime", "posixrules", "Europe//London"])
    def test_a_timezone_that_is_not_a_zone_name_is_refused(self, migrate, value):
        steps = migrate.identity_steps(migrate.Identity(timezone=value), ["identity.timezone"], hosts_text="")
        assert len(steps) == 1 and steps[0].warn and steps[0].write == ()

    # Round three: a link deeper inside a chosen item, below what
    # _confined checks and above any existing file collisions would see.
    def test_a_member_below_a_symlink_inside_a_chosen_item_is_refused(self, migrate, tmp_path):
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/etc/sudoers.d").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents").mkdir(parents=True)
        (tmp_path / "t/home/alice/Documents/sub").symlink_to(tmp_path / "t/etc/sudoers.d")
        items = [migrate.Item("docs", "home.files", "Documents", paths=("home/olduser/Documents",))]
        listing = "home/olduser/Documents/\nhome/olduser/Documents/sub/evil\n"
        with pytest.raises(ValueError, match="Documents/sub/evil"):
            migrate.plan_archive(self._inventory(migrate, items), ["docs"], tmp_path / "a.tar.zst", listing, target, STAMP)
        assert not (tmp_path / "t/etc/sudoers.d/evil").exists()

    def test_a_member_below_a_symlink_that_stays_inside_the_home_passes(self, migrate, tmp_path):
        target = self._target(migrate, tmp_path / "t")
        (tmp_path / "t/home/alice/Documents").mkdir(parents=True)
        (tmp_path / "t/home/alice/Downloads").mkdir()
        (tmp_path / "t/home/alice/Documents/sub").symlink_to("../Downloads")
        items = [migrate.Item("docs", "home.files", "Documents", paths=("home/olduser/Documents",))]
        listing = "home/olduser/Documents/\nhome/olduser/Documents/sub/evil\n"
        steps = migrate.plan_archive(self._inventory(migrate, items), ["docs"], tmp_path / "a.tar.zst", listing, target, STAMP)
        assert steps[0].argv[0] == "tar" and steps[0].move_aside == ()
