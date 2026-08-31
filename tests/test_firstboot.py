"""Checks on the artefacts shipped into the image.

These files run on the stick, not on the build host, so nothing here can execute
them. What can be verified is that they parse, and that the systemd unit carries
the directives the design depends on.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RESOURCES = Path(__file__).parent.parent / "portlin" / "resources" / "firstboot"
WIZARD = RESOURCES / "portlin-firstboot"
UNIT = RESOURCES / "portlin-firstboot.service"
FINALISE = RESOURCES / "portlin-finalise-encryption"


class TestWizardScript:
    def test_it_parses(self):
        # A syntax error here is only discovered on a stranger's laptop, at the
        # exact moment the stick is supposed to be creating their account.
        ast.parse(WIZARD.read_text())

    def test_it_has_a_python3_shebang(self):
        assert WIZARD.read_text().startswith("#!/usr/bin/env python3")

    def test_it_imports_nothing_outside_the_standard_library(self):
        # The image has no pip and no venv. A third-party import would make the
        # wizard fail on every stick.
        tree = ast.parse(WIZARD.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        allowed = {
            "__future__", "grp", "os", "re", "signal", "subprocess", "sys",
            "traceback", "pathlib",
        }
        assert imported <= allowed, f"unexpected imports: {imported - allowed}"

    def test_it_removes_the_sentinel_only_after_applying_settings(self):
        source = WIZARD.read_text()
        apply_position = source.index("apply_account(username")
        sentinel_position = source.index("SENTINEL.unlink")
        assert apply_position < sentinel_position

    def test_cancelling_leaves_the_sentinel_in_place(self):
        # Otherwise a cancelled setup strands the user with no account and no
        # second chance, on a stick that boots straight to a login screen.
        source = WIZARD.read_text()
        cancel_handler = source[source.index("except Cancelled:"):source.index("except Exception:")]
        assert "unlink" not in cancel_handler

    def test_a_crash_leaves_the_sentinel_in_place(self):
        # Sliced between two markers unique to main(), because "return 0" occurs
        # in several helpers and anchoring on it silently produced an empty
        # string that trivially satisfied every assertion below.
        source = WIZARD.read_text()
        main_body = source[source.index("def main()"):]
        crash_handler = main_body[main_body.index("except Exception:"):main_body.index("finally:")]
        assert "unlink" not in crash_handler
        assert "log(detail)" in crash_handler

    def test_it_rebuilds_the_initramfs_after_a_keymap_change_on_encrypted_sticks(self):
        # The LUKS prompt lives in the initramfs and would otherwise stay on a US
        # layout forever, making a symbol-heavy passphrase impossible to type.
        source = WIZARD.read_text()
        assert "refresh_initramfs_for_keymap" in source
        assert "update-initramfs" in source

    def test_it_never_passes_a_passphrase_as_an_argument(self):
        source = WIZARD.read_text()
        assert "luksChangeKey" in source
        change_key_line = next(
            line for line in source.splitlines() if "luksChangeKey" in line and "run(" in line
        )
        assert "password" not in change_key_line
        assert "key-file" not in change_key_line

    def test_console_commands_cannot_block_the_wizard(self):
        # plymouth quit blocks trying to reach a daemon that has already exited,
        # which hangs the wizard before it draws anything: a total, silent
        # failure to boot. Every one of these needs a timeout.
        source = WIZARD.read_text()
        body = source[source.index("def claim_console"):source.index("def log(")]
        assert "timeout=" in body
        assert "TimeoutExpired" in body
        # And none of them may be invoked directly, bypassing the guard.
        for command in ("plymouth", "chvt", "setterm"):
            assert f'subprocess.run(["{command}"' not in body

    def test_it_silences_all_three_console_writers(self):
        # Kernel printk, systemd status lines and plymouth each write to tty1
        # during boot and each needs its own silencer, or they draw straight
        # through the wizard's dialogs and it looks like a crash.
        source = WIZARD.read_text()
        assert "setterm" in source
        assert "signal.SIGRTMIN" in source
        assert "plymouth" in source

    def test_it_releases_plymouth_rather_than_killing_it(self):
        # The wizard needs the console, not plymouth's death. Killing the daemon
        # deadlocks plymouth-quit-wait and with it the whole boot.
        source = WIZARD.read_text()
        body = source[source.index("def claim_console"):source.index("def log(")]
        # Assert on the call, not on prose: earlier versions of this test matched
        # comments describing the behaviour being removed.
        assert 'attempt(["plymouth", "deactivate"])' in body
        assert 'attempt(["plymouth", "quit"])' not in body

    def test_it_always_hands_the_console_back(self):
        # Leaving the console permanently muted would be far worse than the
        # cosmetic problem being solved, so the restore must be in a finally.
        source = WIZARD.read_text()
        main_body = source[source.index("def main()"):]
        assert "finally:" in main_body
        restore = main_body[main_body.index("finally:"):]
        assert "claim_console(False)" in restore

    def test_dialog_height_grows_with_the_content(self):
        # whiptail clips silently rather than scrolling, so a fixed height eats
        # the end of any longer message. The summary screen lost both its last
        # row and the question its buttons were answering.
        import re as _re

        source = WIZARD.read_text()
        namespace: dict = {}
        body = source[source.index("def _box_height"):source.index("def message(")]
        exec(body, namespace)
        box_height = namespace["_box_height"]

        summary = "\n".join(["line"] * 9)
        assert box_height(summary) > 12, "a nine-line summary must not use the old height"
        assert box_height("one line") == 12, "short messages keep a sane minimum"
        # Never taller than an 80x25 console.
        assert box_height("\n".join(["x"] * 200)) <= 22

    def test_message_and_confirm_size_themselves_to_their_content(self):
        source = WIZARD.read_text()
        for call in ("--msgbox", "--yesno"):
            line = next(l for l in source.splitlines() if call in l)
            assert "_box_height" in line, f"{call} still uses a fixed height: {line.strip()}"

    def test_expansion_grows_the_layers_outside_in(self):
        # Each layer can only grow into space the layer beneath has claimed, so
        # partition then LUKS mapping then filesystem is the only correct order.
        source = WIZARD.read_text()
        body = source[source.index("def apply_expand"):source.index("def step_autologin")]
        assert body.index("growpart") < body.index("cryptsetup") < body.index("resize2fs")

    def test_expansion_runs_before_the_rest_of_setup(self):
        # Otherwise the account and locale data land in the few gigabytes the
        # image shipped with, and get copied again when the filesystem grows.
        source = WIZARD.read_text()
        wizard_body = source[source.index("def wizard()"):source.index("def main()")]
        assert wizard_body.index("apply_expand()") < wizard_body.index("apply_account(")

    def test_expansion_tolerates_growpart_reporting_no_change(self):
        # growpart exits non-zero with NOCHANGE when the partition already fills
        # the disk, which is a normal outcome, not a failure worth alarming over.
        source = WIZARD.read_text()
        assert "NOCHANGE" in source

    def test_expansion_counts_space_inside_the_partition_too(self):
        # An interrupted expansion grows the partition and stops, leaving the
        # filesystem small inside a full-size partition and zero free space
        # after it. Checking only the space after the partition would decide
        # there is nothing to do and never offer again.
        source = WIZARD.read_text()
        body = source[source.index("def step_expand"):source.index("def apply_expand")]
        assert "_unused_inside_partition" in body
        assert "_free_space_bytes" in body

    def test_expansion_is_skipped_when_there_is_nothing_to_claim(self):
        source = WIZARD.read_text()
        assert "1024**3" in source, "expected a floor below which expanding is pointless"

    def test_free_space_is_read_from_sysfs_not_parsed_from_a_command(self):
        # The previous version shelled out to partx with a malformed argument.
        # The command failed, the empty output parsed as zero free space, and
        # expansion was silently never offered -- no error, nothing in the log.
        source = WIZARD.read_text()
        body = source[source.index("def _free_space_bytes"):source.index("def _unused_inside_partition")]
        # The property that matters is that it runs no command at all, so there
        # is no output to misparse. Checking for the absence of "partx" would
        # only catch the docstring explaining why it is gone.
        assert "subprocess" not in body
        assert "/sys/class/block" in source

    def test_free_space_maths_matches_real_sysfs_values(self):
        # Numbers measured from an actual 8 GB portlin image on a loop device.
        source = WIZARD.read_text()
        namespace = {"Path": Path}
        exec(source[source.index("def _sectors"):source.index("def step_expand")], namespace)
        namespace["_sectors"] = lambda name, attr: {
            ("sda", "size"): 62914560,      # 30 GiB stick
            ("sda4", "start"): 3149824,
            ("sda4", "size"): 13627359,     # root from an 8 GB image
        }.get((name, attr), 0)
        exec(source[source.index("def _free_space_bytes"):source.index("def step_expand")], namespace)
        free = namespace["_free_space_bytes"]("/dev/sda", "/dev/sda4")
        assert 21 * 1024**3 < free < 23 * 1024**3, f"expected ~22 GiB, got {free}"

    def test_no_free_space_when_the_image_already_fills_the_disk(self):
        source = WIZARD.read_text()
        namespace = {"Path": Path}
        exec(source[source.index("def _sectors"):source.index("def step_expand")], namespace)
        namespace["_sectors"] = lambda name, attr: {
            ("loop0", "size"): 16777216,
            ("loop0p4", "start"): 3149824,
            ("loop0p4", "size"): 13627359,
        }.get((name, attr), 0)
        exec(source[source.index("def _free_space_bytes"):source.index("def step_expand")], namespace)
        assert namespace["_free_space_bytes"]("/dev/loop0", "/dev/loop0p4") == 0

    def test_a_skipped_expansion_says_why(self):
        # Silently vanishing is how this bug survived: the step simply never
        # appeared and left nothing behind to explain itself.
        source = WIZARD.read_text()
        body = source[source.index("def step_expand"):source.index("def apply_expand")]
        assert "log(" in body

    def test_the_backing_device_is_found_through_sysfs_slaves(self):
        # dm-crypt devices have no "device" link in sysfs, so lsblk -o PKNAME
        # returns an empty string for them. Every lookup built on it silently
        # produced nothing, and expansion was never offered on encrypted sticks.
        source = WIZARD.read_text()
        body = source[source.index("def _backing_partition"):source.index("def _parent_disk")]
        # Asserting on code, not prose. Three tests in this file have now failed
        # because their assertion matched a comment explaining the behaviour
        # being removed; the no-subprocess test below covers "lsblk is gone".
        assert 'slaves = node / "slaves"' in body

    def test_device_lookup_does_not_rely_on_udev_symlinks(self):
        # Without udev, cryptsetup creates a real device node at
        # /dev/mapper/<name> rather than a symlink to /dev/dm-N, so resolve()
        # returns the path unchanged and points at sysfs entries that do not
        # exist. The device number identifies it either way.
        source = WIZARD.read_text()
        body = source[source.index("def _sysfs_node"):source.index("def _parent_disk")]
        assert "/sys/dev/block/" in body
        assert "st_rdev" in body

    def test_nothing_in_device_discovery_parses_command_output(self):
        # Two separate bugs here came from misparsing a command: partx with a
        # malformed flag, and lsblk returning empty for dm devices. sysfs has
        # neither failure mode.
        source = WIZARD.read_text()
        body = source[source.index("def _backing_partition"):source.index("def _unused_inside_partition")]
        # _unused_inside_partition is excluded deliberately: filesystem size has no
        # sysfs equivalent, so dumpe2fs is the only way to ask.
        assert "subprocess" not in body

    def test_the_root_device_is_discovered_not_assumed(self):
        # The stick is a different device on every machine it is plugged into,
        # so anything hardcoded here is wrong by the second boot.
        source = WIZARD.read_text()
        body = source[source.index("def _root_devices"):source.index("def _free_space_bytes")]
        assert "findmnt" in body
        assert "lsblk" in body
        assert "/dev/sda" not in body

    def test_it_makes_its_console_the_visible_one(self):
        # TTYPath attaches the wizard to tty1; it does not put tty1 in front of
        # the user. Without an explicit switch the wizard draws onto a console
        # nobody is looking at and the machine appears to have hung mid-boot.
        assert "chvt" in WIZARD.read_text()

    def test_it_refuses_to_run_as_a_normal_user(self):
        assert "os.geteuid() != 0" in WIZARD.read_text()

    def test_it_filters_groups_against_the_ones_that_exist(self):
        # useradd fails outright on an unknown group, and the set differs between
        # Debian releases.
        source = WIZARD.read_text()
        assert "grp.getgrall()" in source

    def test_usernames_are_validated_against_a_pattern(self):
        source = WIZARD.read_text()
        assert "USERNAME_RE" in source
        pattern = re.search(r'USERNAME_RE = re\.compile\(r"(.+?)"\)', source).group(1)
        compiled = re.compile(pattern)
        assert compiled.match("alice")
        assert compiled.match("_svc")
        assert not compiled.match("Alice")
        assert not compiled.match("1alice")
        assert not compiled.match("has space")
        assert not compiled.match("")

    def test_hostnames_are_validated_against_a_pattern(self):
        source = WIZARD.read_text()
        pattern = re.search(r'HOSTNAME_RE = re\.compile\(r"(.+?)"\)', source).group(1)
        compiled = re.compile(pattern)
        assert compiled.match("portlin")
        assert compiled.match("my-stick-01")
        assert not compiled.match("-leading")
        assert not compiled.match("trailing-")
        assert not compiled.match("has space")


class TestEncryptOnFirstBoot:
    """The initramfs-side offer to encrypt an unencrypted stick."""

    HOOK = RESOURCES / "portlin-encrypt.hook"
    LOCAL_TOP = RESOURCES / "portlin-encrypt.local-top"

    def test_both_scripts_exist(self):
        assert self.HOOK.exists()
        assert self.LOCAL_TOP.exists()

    def test_they_answer_the_prereqs_probe(self):
        # initramfs-tools calls every script with "prereqs" first; one that does
        # not answer and exit is run at the wrong point in boot, or not at all.
        for script in (self.HOOK, self.LOCAL_TOP):
            body = script.read_text()
            assert "prereqs)" in body
            assert "exit 0" in body

    def test_encryption_only_runs_when_explicitly_asked(self):
        # The flag rides the kernel command line, so the initramfs does not have
        # to mount a filesystem just to find out whether to ask.
        assert "portlin.encrypt=ask" in self.LOCAL_TOP.read_text()
        assert "/proc/cmdline" in self.LOCAL_TOP.read_text()

    def test_it_waits_for_the_root_device_to_appear(self):
        # local-top runs before initramfs-tools waits for the root device, and a
        # USB stick takes a second or two to enumerate. A virtio disk in a VM
        # appears instantly, which is why this only ever failed on hardware.
        body = self.LOCAL_TOP.read_text()
        assert "while [" in body and "resolve_root" in body
        assert "sleep" in body

    def test_it_tries_every_form_the_root_parameter_can_take(self):
        body = self.LOCAL_TOP.read_text()
        for form in ("UUID=*)", "LABEL=*)", "/dev/*)"):
            assert form in body

    def test_it_falls_back_past_the_by_uuid_symlink(self):
        # Those symlinks are udev's work and may not exist yet at this point.
        body = self.LOCAL_TOP.read_text()
        assert "blkid -U" in body
        assert "resolve_device" in body

    def test_giving_up_is_bounded_and_says_so(self):
        body = self.LOCAL_TOP.read_text()
        assert "after 30s" in body

    def test_it_refuses_to_reencrypt_an_existing_container(self):
        # Running reencrypt over a LUKS device would destroy it.
        body = self.LOCAL_TOP.read_text()
        already = body[body.index("if cryptsetup isLuks"):body.index("say \"\"\nsay \"===")]
        assert "reencrypt" not in already

    def test_it_recovers_a_drive_encrypted_by_an_unfinished_setup(self):
        # Encrypted but not finalised means no crypttab exists yet, so nothing
        # else can unlock the root and the boot dies in an initramfs shell.
        # Cancelling a setup wizard must not brick the stick.
        body = self.LOCAL_TOP.read_text()
        already = body[body.index("if cryptsetup isLuks"):]
        assert "setup did not finish" in already
        assert "cryptsetup open" in already.split("reencrypt")[0]
        assert "ROOT=/dev/mapper/portlin_root" in already.split("reencrypt")[0]

    def test_it_makes_room_for_the_luks_header(self):
        # A header can only be added by shifting the filesystem forward, which is
        # exactly why this cannot be done on a mounted root.
        assert "--reduce-device-size" in self.LOCAL_TOP.read_text()

    def test_it_shrinks_the_filesystem_before_inserting_the_header(self):
        # The LUKS2 header goes at the front, so the filesystem ends up in a
        # smaller device. ext4 records its own size and does not notice, so
        # without shrinking first it believes it is larger than its container
        # and fsck drops the machine to an initramfs shell on the next boot.
        body = self.LOCAL_TOP.read_text()
        assert body.index("resize2fs") < body.index("cryptsetup reencrypt")
        assert body.index("e2fsck") < body.index("resize2fs")

    def test_the_shrink_matches_the_space_the_header_takes(self):
        body = self.LOCAL_TOP.read_text()
        assert "HEADER_BYTES=33554432" in body
        assert "--reduce-device-size 32M" in body

    def test_a_failed_shrink_leaves_the_drive_bootable(self):
        # Aborting before the header is written means the plaintext filesystem
        # is untouched and still mounts.
        body = self.LOCAL_TOP.read_text()
        shrink = body[body.index("resize2fs"):body.index("cryptsetup reencrypt")]
        assert "exit 0" in shrink

    def test_the_hook_ships_the_resize_tools(self):
        # resize2fs is never in a stock initramfs.
        body = self.HOOK.read_text()
        assert "resize2fs" in body
        assert "e2fsck" in body

    def test_it_caps_the_kdf_for_low_ram_machines(self):
        assert "--pbkdf-memory 262144" in self.LOCAL_TOP.read_text()

    def test_the_passphrase_is_never_an_argument(self):
        body = self.LOCAL_TOP.read_text()
        assert "--key-file -" in body
        assert "--key-file $" not in body

    def test_it_requires_the_passphrase_twice(self):
        # A typo here produces a stick nobody can ever open.
        body = self.LOCAL_TOP.read_text()
        assert "pass1" in body and "pass2" in body
        assert '"$pass1" != "$pass2"' in body

    def test_declining_leaves_the_drive_untouched(self):
        body = self.LOCAL_TOP.read_text()
        assert "Continuing without encryption" in body

    def test_a_failure_leaves_the_drive_bootable(self):
        # Reencryption failing must not strand the user; the plaintext
        # filesystem is still there and still boots.
        body = self.LOCAL_TOP.read_text()
        assert "continuing unencrypted" in body

    def test_it_redirects_the_rest_of_boot_at_the_unlocked_mapping(self):
        # initramfs-tools sources this script, so reassigning ROOT is what makes
        # the boot continue into the container rather than the raw partition.
        body = self.LOCAL_TOP.read_text()
        assert "ROOT=/dev/mapper/portlin_root" in body

    def test_the_hook_forces_cryptsetup_into_the_initramfs(self):
        # cryptsetup-initramfs only includes it when an encrypted volume already
        # exists -- and the one boot that needs it is the boot before any
        # encryption exists.
        assert "copy_exec" in self.HOOK.read_text()
        assert "cryptsetup" in self.HOOK.read_text()

    def test_the_wizard_only_reports_whether_finalisation_already_happened(self):
        # The work moved to portlin-finalise-encryption.service, which runs on
        # every boot and outlives the wizard disabling itself. The wizard now
        # only reads the breadcrumb it leaves, so it can tell the user.
        body = WIZARD.read_text()
        finalise = body[body.index("def finalise_encryption"):body.index("def step_autologin")]
        assert "/run/portlin/finalised" in finalise
        assert "crypttab" not in finalise

    def test_the_finaliser_makes_the_encryption_permanent(self):
        # The initramfs can unlock for one boot; crypttab, the rebuilt initramfs
        # and the dropped flag are what make it survive a reboot.
        body = FINALISE.read_text()
        assert "crypttab" in body
        assert "update-initramfs" in body
        assert "portlin.encrypt=ask" in body
        assert "update-grub" in body

    def test_finalisation_keys_off_observable_state(self):
        # Not off a flag file that a crash could lose, leaving a stick encrypted
        # but unable to unlock itself.
        body = FINALISE.read_text()
        assert "findmnt" in body
        assert "/dev/mapper/" in body

    def test_the_device_lookup_helpers_have_not_drifted_from_the_wizard(self):
        # The finaliser carries its own copy of _sysfs_node and _backing_partition
        # rather than importing the wizard. A brief once respecified _sysfs_node
        # with the plain resolve()-based lookup that only works under udev, which
        # made finalisation silently fail on the exact boot it matters most: the
        # one right after the initramfs created the container, with no udev yet.
        # This pins the two copies together so they cannot drift apart again.
        wizard = WIZARD.read_text()
        finalise = FINALISE.read_text()

        def function_body(source: str, name: str) -> str:
            start = source.index(f"def {name}")
            end = source.index("\ndef ", start + 1)
            return source[start:end]

        for name in ("_sysfs_node", "_backing_partition"):
            assert function_body(wizard, name) == function_body(finalise, name), (
                f"{name} has drifted between the wizard and the finaliser"
            )


class TestSystemdUnit:
    @pytest.fixture
    def unit(self) -> str:
        return UNIT.read_text()

    def test_it_is_gated_on_the_sentinel(self, unit):
        assert "ConditionPathExists=/var/lib/portlin/firstboot-pending" in unit

    def test_it_runs_before_the_display_manager(self, unit):
        # LightDM must wait, or the greeter appears with no accounts to offer.
        assert "Before=display-manager.service" in unit

    def test_it_owns_tty1(self, unit):
        assert "TTYPath=/dev/tty1" in unit
        assert "Conflicts=getty@tty1.service" in unit

    def test_it_gets_a_real_terminal_for_input(self, unit):
        assert "StandardInput=tty-force" in unit

    def test_it_does_not_time_out_while_someone_is_typing(self, unit):
        assert "TimeoutStartSec=infinity" in unit

    def test_it_is_a_oneshot_that_stays_active(self, unit):
        assert "Type=oneshot" in unit
        assert "RemainAfterExit=yes" in unit

    def test_the_unit_switches_the_visible_console_to_tty1(self, unit):
        assert "chvt 1" in unit, "the wizard must be on the console the user sees"

    def test_the_unit_does_not_order_against_plymouth_quit_wait(self, unit):
        # plymouth-quit-wait waits for plymouth to die; plymouth dies when the
        # display manager starts; the display manager waits for this unit.
        # Ordering after it closes that loop and the boot never completes.
        assert "After=plymouth-quit-wait" not in unit
        assert "ExecStartPre=-/usr/bin/plymouth" not in unit

    def test_console_setup_failures_do_not_abort_setup(self, unit):
        # Both are prefixed with '-' so a machine without plymouth or kbd still
        # gets a working wizard.
        for line in unit.splitlines():
            if "ExecStartPre" in line:
                assert "=-" in line, f"should tolerate failure: {line}"

    def test_it_is_installable(self, unit):
        assert "WantedBy=multi-user.target" in unit


class TestKeyringUnderAutologin:
    """Autologin and an encrypted login keyring cannot both be had.

    LightDM authenticates autologin sessions through the lightdm-autologin PAM
    service, which satisfies auth with pam_permit rather than including
    common-auth. That substitution is what lets a session start with nothing
    typed, and it is also why pam_gnome_keyring never sees a password to unlock
    the login keyring with. The daemon still starts, so every libsecret client
    meets a locked keyring and asks for the login password instead.
    """

    @pytest.fixture
    def source(self):
        return WIZARD.read_text()

    def test_the_keyring_is_only_unlocked_when_the_stick_is_encrypted(self, source):
        # The whole trade is that LUKS takes over the job the keyring password
        # was doing. Without LUKS there is no such handover, and a passwordless
        # keyring would leave saved passwords readable to whoever finds the
        # stick -- on a device whose entire purpose is being carried around.
        applying = source[source.index("    apply_autologin(username, autologin)"):source.index("SENTINEL.unlink")]
        assert "apply_keyring_autounlock(" in applying, "nothing opens the keyring at all"
        guard = applying[:applying.index("apply_keyring_autounlock(")]
        assert "luks_device" in guard, "the keyring must not be opened up on an unencrypted stick"
        assert "autologin" in guard, "a password login unlocks the keyring by itself"

    def test_the_keyring_password_is_never_an_argument(self, source):
        # Same rule the LUKS passphrase already follows: anything in argv is
        # readable from /proc by every user on the machine.
        body = source[source.index("def apply_keyring_autounlock"):source.index("def refresh_initramfs_for_keymap")]
        daemon_call = next(l for l in body.splitlines() if "GNOME_KEYRING_DAEMON" in l and "run(" in l or "--login" in l)
        assert "--password" not in body and "-p " not in daemon_call
        assert "stdin=" in body, "the password must go in on stdin, empty"

    def test_an_unencrypted_stick_says_what_autologin_costs(self, source):
        # This bug was invisible: autologin was offered, accepted, and the
        # consequence only showed up days later as browsers nagging for a
        # password the user had never knowingly set.
        body = source[source.index("def step_autologin"):source.index("# ---", source.index("def step_autologin"))]
        assert "encrypted" in body, "the warning has to know whether LUKS is there"
        assert "keyring" in body.lower(), "the cost has to be named where the choice is made"

    def test_it_creates_the_keyring_by_the_same_route_pam_uses(self, tmp_path):
        # Writing gnome-keyring's on-disk format by hand would mean this script
        # implementing key derivation. --login is the entry point PAM itself
        # feeds, so an empty stdin produces exactly the keyring wanted.
        source = WIZARD.read_text()
        home = tmp_path / "home" / "ada"
        keyring = home / ".local/share/keyrings/login.keyring"
        daemon = tmp_path / "gnome-keyring-daemon"
        daemon.write_text("")
        calls: list = []

        def fake_run(argv, *, check=True, stdin=None):
            calls.append((argv, stdin))
            if "--login" in argv:
                keyring.parent.mkdir(parents=True, exist_ok=True)
                keyring.write_bytes(b"GnomeKeyring\n\r\0\n")
            return None

        class _os:
            class path:
                expanduser = staticmethod(lambda p: str(home))

        namespace = {
            "Path": Path, "os": _os, "run": fake_run, "log": lambda *a: None,
            "GNOME_KEYRING_DAEMON": str(daemon),
        }
        exec(source[source.index("def apply_keyring_autounlock"):source.index("def refresh_initramfs_for_keymap")], namespace)

        assert namespace["apply_keyring_autounlock"]("ada") is True
        argv, stdin = calls[0]
        assert "--login" in argv, f"expected the --login entry point, got {argv}"
        assert stdin == "", "an empty password is what makes the keyring self-unlocking"
        assert "ada" in argv, "the keyring belongs to the user, not to root"

    def test_it_reports_failure_rather_than_pretending(self, tmp_path):
        # A silent failure here hands back exactly the symptom being fixed.
        source = WIZARD.read_text()
        home = tmp_path / "home" / "ada"
        daemon = tmp_path / "gnome-keyring-daemon"
        daemon.write_text("")

        class _os:
            class path:
                expanduser = staticmethod(lambda p: str(home))

        namespace = {
            "Path": Path, "os": _os, "run": lambda *a, **k: None, "log": lambda *a: None,
            "GNOME_KEYRING_DAEMON": str(daemon),
        }
        exec(source[source.index("def apply_keyring_autounlock"):source.index("def refresh_initramfs_for_keymap")], namespace)
        assert namespace["apply_keyring_autounlock"]("ada") is False

    def test_a_stick_with_no_keyring_at_all_is_not_a_failure(self, tmp_path):
        # --minimal builds have no gnome-keyring, so nothing can prompt and
        # there is nothing to fix. That is not the same as the fix failing.
        source = WIZARD.read_text()

        class _os:
            class path:
                expanduser = staticmethod(lambda p: str(tmp_path / "home" / "ada"))

        namespace = {
            "Path": Path, "os": _os, "run": lambda *a, **k: None, "log": lambda *a: None,
            "GNOME_KEYRING_DAEMON": str(tmp_path / "absent"),
        }
        exec(source[source.index("def apply_keyring_autounlock"):source.index("def refresh_initramfs_for_keymap")], namespace)
        assert namespace["apply_keyring_autounlock"]("ada") is True
