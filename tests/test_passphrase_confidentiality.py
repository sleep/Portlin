"""The passphrase must never be written to the stick.

The first-boot keyscript keeps the disk passphrase for a few minutes so the
expansion can resize the LUKS mapping without asking for it a second time. That
is only defensible while the copy stays in RAM, so the properties that keep it
there are asserted here rather than left implicit in four different files.

Each of these holds for its own separate reason elsewhere in the tree -- RESUME
is about boot stalls, zram is about flash wear -- which is exactly why they are
restated here. A future change that adds a swap partition for low-memory
machines would be reasonable on its own terms and would silently put the
passphrase on disk, and the test that stops it should say so.
"""

from __future__ import annotations

from pathlib import Path

from portlin import layout, templates

GIB = 1024**3
RESOURCES = Path(__file__).parent.parent / "portlin" / "resources" / "firstboot"
KEYSCRIPT = RESOURCES / "portlin-stash-passphrase"
WIZARD = RESOURCES / "portlin-firstboot"
ENCRYPT_HOOK = RESOURCES / "portlin-encrypt.local-top"


class TestTheStashStaysInRam:
    def test_the_stash_lives_under_run(self):
        # /run is the one tmpfs the initramfs moves onto the real root at pivot
        # (initramfs-tools' init does `mount -n -o move /run ${rootmnt}/run`),
        # so it is the only path that is both writable before root is mounted
        # and still readable by the wizard afterwards, without ever being a file
        # on the stick.
        assert 'STASH_DIR=/run/portlin' in KEYSCRIPT.read_text()
        assert 'STASH = Path("/run/portlin/luks-pass")' in WIZARD.read_text()

    def test_the_keyscript_writes_to_exactly_one_place(self):
        # Any second redirection in this script is a second copy of the
        # passphrase, and the whole argument for keeping it rests on there
        # being one, in RAM, deleted on first use.
        body = KEYSCRIPT.read_text()
        redirections = [
            line.strip()
            for line in body.splitlines()
            if ">" in line and not line.lstrip().startswith("#") and "2>" not in line
        ]
        assert len(redirections) == 1, f"expected one write, found {redirections}"
        assert "$STASH" in redirections[0]


class TestTheHookStashesOnTheSameTerms:
    """The encrypt hook writes the same stash on the boot it creates the
    container, so the two writers have to be held to one standard."""

    def test_it_stashes_to_the_same_place_the_keyscript_does(self):
        # One path, because the wizard reads one path. The two writers reaching
        # different files would present as the stash silently never working.
        assert "/run/portlin/luks-pass" in ENCRYPT_HOOK.read_text()

    def test_the_passphrase_is_written_to_exactly_one_place(self):
        # Everything else this script redirects is either console output, which
        # never sees the passphrase, or the empty breadcrumb it leaves for the
        # wizard. Any other write carrying a variable is a second copy, and the
        # whole argument for keeping one rests on there being exactly one.
        carrying = [
            line.strip()
            for line in ENCRYPT_HOOK.read_text().splitlines()
            if ">" in line
            and not line.lstrip().startswith("#")
            and not line.lstrip().startswith(": >")
            and "2>" not in line
            and ">&" not in line
            and "$CONSOLE" not in line
        ]
        assert len(carrying) == 1, f"expected one write, found {carrying}"
        assert "/run/portlin/luks-pass" in carrying[0]

    def test_nothing_it_writes_lands_on_the_stick(self):
        code = [
            line for line in ENCRYPT_HOOK.read_text().splitlines()
            if ">" in line and not line.lstrip().startswith("#")
        ]
        for line in code:
            for durable in ("/var/", "/etc/", "/root/", "/tmp/"):
                assert durable not in line, f"the stash must not touch {durable}"

    def test_the_stash_is_unreadable_to_anyone_but_root(self):
        assert "umask 077" in ENCRYPT_HOOK.read_text()


class TestNothingCanPageTheStashToDisk:
    """tmpfs is RAM until something gives the kernel somewhere to page it."""

    def test_the_stick_has_no_swap_partition(self):
        # A swap partition is disk. Anything in tmpfs, the stash included, can
        # be paged out to it under memory pressure and left there in the clear.
        plan = layout.plan_partitions(32 * GIB, encrypted=True)
        types = {partition.typecode for partition in plan.partitions}
        assert "8200" not in types, "a swap partition can hold paged-out tmpfs"

    def test_fstab_activates_no_swap(self):
        # Same hazard by a different route: a swapfile or a swap device named
        # in fstab is still disk.
        rendered = templates.render_fstab(root_uuid="a", boot_uuid="b", esp_uuid="c")
        assert "swap" not in rendered.lower()

    def test_swap_is_compressed_ram_rather_than_a_device(self):
        # zram is a block device backed by RAM, so pages pushed into it never
        # leave memory. It is what makes having no disk swap survivable on a
        # machine with little of it.
        assert "ALGO=" in templates.render_zram_conf()

    def test_hibernation_has_no_target(self):
        # A hibernation image is the whole of RAM written to disk, stash and
        # all. RESUME=none leaves the initramfs nothing to resume from.
        assert "RESUME=none" in templates.render_initramfs_conf()


class TestTheLogsNeverSeeIt:
    def test_the_command_logger_records_arguments_but_not_input(self):
        # The wizard logs every command it runs to a file on the stick. The
        # passphrase reaches cryptsetup on stdin precisely so that this log,
        # and /proc, only ever see "cryptsetup resize --key-file -".
        source = WIZARD.read_text()
        body = source[source.index("def run(argv"):source.index("def _whiptail")]
        logged = [line for line in body.splitlines() if "log(" in line]
        assert logged, "the wizard is expected to log the commands it runs"
        assert not any("stdin" in line for line in logged), (
            "the command log must never render stdin, which carries the passphrase"
        )

    def test_the_passphrase_only_ever_travels_by_stdin(self):
        # Every cryptsetup call that needs it uses --key-file - and hands the
        # bytes over a pipe. An argv form would put it in /proc for every user
        # on the machine to read.
        source = WIZARD.read_text()
        for line in source.splitlines():
            if "cryptsetup" in line and "resize" in line:
                assert "--key-file" not in line or '"-"' in line
