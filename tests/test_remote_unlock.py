"""Remote unlock: an SSH server in the initramfs, switched by one file.

Three tiers touch it, and nothing fails loudly when they drift apart. The
gate hook and the boot script are frozen and run inside the initramfs; the
first-boot wizard is frozen and writes the switch; portlin-remote-unlock is
updatable and writes the same switch years later. They agree on a handful of
paths, a port and the spelling of "on", and this is where that agreement is
held, because the failure it prevents is a stick that answers SSH before it
has unlocked, or one that was told to and never does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import load_tool
from portlin import install, templates
from portlin.config import BuildConfig, WriteConfig
from portlin.install import write_stick
from portlin.layout import GIB
from portlin.rootfs import build_rootfs
from test_firstboot import WIZARD, load_wizard, module_constant, summary_state

FIRSTBOOT = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "firstboot"
HOOK = FIRSTBOOT / "portlin-remote-unlock.hook"
PREMOUNT = FIRSTBOOT / "portlin-remote-unlock.init-premount"

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq4u0J8bq4v6X5ZkZcOq7Zf9nq0q7mZr0M3o9cQ5x2y sam@laptop"
RSA_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7" + "a" * 500 + "= sam@desk"


def _tool():
    return load_tool("portlin-remote-unlock")


class TestOneSwitchThreeReaders:
    """The paths and the spelling every tier has to agree on."""

    def test_the_switch_file_is_the_same_everywhere(self):
        conf = templates.REMOTE_UNLOCK_CONF
        assert f'REMOTE_UNLOCK_CONF = Path("{conf}")' in WIZARD.read_text()
        assert _tool().CONF == Path(conf)
        assert f"CONF={conf}" in HOOK.read_text()
        assert f"CONF={conf}" in PREMOUNT.read_text()

    def test_on_is_spelled_the_way_the_hook_reads_it(self):
        # The template and both writers emit enabled=1; the hook and the boot
        # script grep for exactly that line and nothing looser.
        assert "enabled=1" in templates.render_remote_unlock_conf(enabled=True)
        assert "enabled=0" in templates.render_remote_unlock_conf()
        assert "grep -qsx 'enabled=1'" in HOOK.read_text()
        assert "grep -qsx 'enabled=1'" in PREMOUNT.read_text()
        wizard = WIZARD.read_text()
        body = wizard[wizard.index("def write_remote_unlock_conf"):wizard.index("def apply_remote_unlock")]
        assert 'f"enabled={int(enabled)}"' in body

    def test_the_keys_live_where_dropbears_hook_reads_them(self):
        path = "/etc/dropbear/initramfs/authorized_keys"
        assert f'REMOTE_UNLOCK_KEYS = Path("{path}")' in WIZARD.read_text()
        assert _tool().KEYS == Path(path)
        assert f"AUTHORIZED_KEYS={path}" in HOOK.read_text()

    def test_the_host_key_is_the_same_file_for_write_the_wizard_and_the_tool(self):
        path = install.REMOTE_UNLOCK_HOST_KEY
        assert path.startswith("/etc/dropbear/initramfs/dropbear_") and path.endswith("_host_key")
        assert f'REMOTE_UNLOCK_HOST_KEY = Path("{path}")' in WIZARD.read_text()
        assert _tool().HOST_KEY == Path(path)

    def test_the_default_port_is_the_same_everywhere(self):
        port = templates.REMOTE_UNLOCK_PORT
        assert port != 22, "22 would clash with the real sshd's host key in every known_hosts"
        assert module_constant(WIZARD, "REMOTE_UNLOCK_PORT") == port
        assert _tool().DEFAULT_PORT == port
        assert f"PORT={port} ;;" in PREMOUNT.read_text(), "the boot script's fallback port differs"
        assert f"port={port}" in templates.render_remote_unlock_conf()

    def test_the_tool_looks_for_the_hook_write_installs(self):
        assert _tool().GATE_HOOK == Path("/" + install.REMOTE_UNLOCK_HOOK)

    def test_the_wizard_and_the_tool_accept_the_same_keys(self):
        wizard = load_wizard()
        assert wizard.PUBLIC_KEY_RE.pattern == _tool().PUBLIC_KEY_RE.pattern
        assert wizard.GITHUB_NAME_RE.pattern == _tool().GITHUB_NAME_RE.pattern
        # And the hook's own, looser check agrees on every type they accept.
        for key in (KEY, RSA_KEY, "ecdsa-sha2-nistp256 AAAA= x", "sk-ssh-ed25519@openssh.com AAAA= x"):
            assert wizard.PUBLIC_KEY_RE.match(key), key
            assert key.split()[0].startswith(("ssh-", "ecdsa-", "sk-"))


class TestTheFrozenScripts:
    @pytest.fixture(params=[HOOK, PREMOUNT], ids=["hook", "premount"])
    def script(self, request):
        return request.param

    def test_it_runs_under_the_initramfs_shell(self, script):
        assert script.read_text().startswith("#!/bin/sh")

    @pytest.mark.skipif(shutil.which("sh") is None, reason="no sh here")
    def test_it_parses(self, script):
        subprocess.run(["sh", "-n", str(script)], check=True)

    @pytest.mark.skipif(shutil.which("sh") is None, reason="no sh here")
    def test_it_answers_the_prereqs_probe_without_touching_anything(self, script):
        # mkinitramfs and init both call every script with "prereqs" first,
        # on the build host and before /scripts/functions exists. Anything
        # done before that case statement runs there too.
        proc = subprocess.run(["sh", str(script), "prereqs"], capture_output=True, text=True, check=True)
        expected = "udev" if script is PREMOUNT else ""
        assert proc.stdout.strip() == expected

    def test_the_hook_lives_where_it_runs_after_dropbears(self):
        # Both directories run in full, /usr/share first, so /etc is the only
        # place a hook can be sure of coming after a package's.
        assert install.REMOTE_UNLOCK_HOOK.startswith("etc/initramfs-tools/hooks/")

    def test_the_boot_script_takes_dropbears_name(self):
        # Copied into the initramfs after Debian's, over it. Any other name
        # would leave both scripts running and Debian's starting DHCP.
        assert install.REMOTE_UNLOCK_PREMOUNT == "etc/initramfs-tools/scripts/init-premount/dropbear"

    def test_off_takes_the_server_and_every_key_out(self):
        body = HOOK.read_text()
        off = body[body.index("# Off."):]
        assert "dropbear" in off and "rm -f" in off
        assert 'rm -rf "$DESTDIR/etc/dropbear"' in off
        assert ".ssh" in off

    def test_on_carries_the_switch_in_for_the_boot_script(self):
        body = HOOK.read_text()
        on = body[body.index("if grep -qsx 'enabled=1'"):body.index("# Off.")]
        assert 'copy_file config "$CONF"' in on

    def test_on_warns_when_nobody_could_log_in(self):
        on = HOOK.read_text()
        assert "names no key" in on and "portlin-remote-unlock" in on

    def test_the_boot_script_starts_nothing_without_a_link(self):
        body = PREMOUNT.read_text()
        assert "carrier" in body
        run = body[body.index("run_dropbear() {"):body.index("run_dropbear &")]
        assert run.index("wait_for_link || exit 0") < run.index("configure_networking")
        assert run.index("configure_networking") < run.index("exec /sbin/dropbear")

    def test_the_wait_for_a_link_is_short_and_bounded(self):
        body = PREMOUNT.read_text()
        seconds = int(body.split("LINK_WAIT=")[1].split()[0])
        assert 0 < seconds <= 15

    def test_the_network_comes_up_in_the_background(self):
        # So that someone at the keyboard can type the passphrase at once,
        # the way Debian's own script arranges it.
        assert "run_dropbear &" in PREMOUNT.read_text()
        assert 'echo $! >/run/dropbear.pid' in PREMOUNT.read_text()

    def test_a_login_can_only_ask_for_the_passphrase(self):
        body = PREMOUNT.read_text()
        assert "-c /bin/cryptroot-unlock" in body
        assert 'flags="Fs"' in body, "-s is what turns password logins off"
        assert "-j -k" in body, "no port forwarding through a stick's initramfs"

    def test_debians_conffile_still_wins(self):
        body = PREMOUNT.read_text()
        assert body.index(". /etc/dropbear/dropbear.conf") < body.index(': "${DROPBEAR_OPTIONS=')

    def test_it_still_checks_the_switch_itself(self):
        # Belt and braces with the hook: a stale initramfs that somehow kept
        # dropbear must not start it when the file says off.
        body = PREMOUNT.read_text()
        assert body.index("grep -qsx 'enabled=1'") < body.index("run_dropbear &")


class TestWriteStage:
    @pytest.fixture(autouse=True)
    def _run(self, tmp_path, runner, trace):
        cfg = WriteConfig(target=str(tmp_path / "stick.img"), rootfs=tmp_path / "rootfs.tar.zst",
                          encrypt=True, passphrase="correct horse battery staple", image_size=32 * GIB)
        write_stick(cfg, runner)
        self.t = trace(runner)

    def test_both_frozen_scripts_land_before_the_initramfs_is_built(self):
        for path in (install.REMOTE_UNLOCK_HOOK, install.REMOTE_UNLOCK_PREMOUNT):
            assert self.t.before(("write-file", path), ("update-initramfs",)), path

    def test_the_scripts_are_executable(self):
        for path in (install.REMOTE_UNLOCK_HOOK, install.REMOTE_UNLOCK_PREMOUNT):
            line = self.t.lines[self.t.index("write-file", path)]
            assert line.endswith("mode=755"), line

    def test_each_stick_gets_its_own_host_key_before_the_initramfs_is_built(self):
        # dropbear's hook copies whatever host key it finds, so the key has
        # to exist first; and the build strips the shared one the package
        # made, so without this every stick would warn and no server start.
        assert self.t.before(("dropbearkey", "-t", "ed25519"), ("update-initramfs",))
        assert install.REMOTE_UNLOCK_HOST_KEY in self.t.lines[self.t.index("dropbearkey", "-t", "ed25519")]

    def test_the_host_key_is_only_made_where_dropbear_is(self):
        # A rootfs built without the package is skipped, not failed.
        assert self.t.has("test", "-x", "usr/bin/dropbearkey")
        assert self.t.before(("test", "-x", "usr/bin/dropbearkey"), ("dropbearkey", "-t"))


class TestBuildStage:
    @pytest.fixture
    def built(self, tmp_path, runner, trace):
        build_rootfs(BuildConfig(output=tmp_path / "rootfs.tar.zst", work_dir=tmp_path / "work"), runner)
        return trace(runner)

    def test_the_switch_starts_out_off(self, built):
        assert built.has("write-file", templates.REMOTE_UNLOCK_CONF.lstrip("/"))

    def test_the_packages_shared_host_keys_do_not_reach_the_tarball(self, built):
        assert built.has("rm -f /etc/dropbear/initramfs/dropbear_*_host_key")
        assert built.index("rm -f /etc/dropbear/initramfs/dropbear_") < built.token_index("tar", "-cf")


def _services_rows(fb, *, luks="/dev/sda4", hook: Path | None = None, online=False, **answers):
    """The rows the Services screen offers, by key, and the validator it was given."""
    state = fb.State()
    state.username, state.luks_device, state.online = "sam", luks, online
    for key, value in answers.items():
        setattr(state, key, value)
    fb.DROPBEAR_HOOK = str(hook) if hook else "/nonexistent/dropbear"
    fb.SSHD, fb.UFW = "/nonexistent/sshd", "/nonexistent/ufw"
    offered, given = {}, {}

    def settings(title, text, rows, validate=None, **kwargs):
        offered.update({row.key: row for row in rows})
        given["validate"] = validate
        return {row.key: row.value for row in rows}

    fb.ui.settings = settings
    shown = fb.step_services(state)
    return offered, given.get("validate"), shown, state


class TestTheWizardOffersIt:
    @pytest.fixture
    def hook(self, tmp_path):
        path = tmp_path / "dropbear"
        path.write_text("")
        return path

    def test_offered_on_an_encrypted_stick_that_has_dropbear(self, hook):
        rows, validate, shown, _ = _services_rows(load_wizard(), hook=hook)
        assert shown and "remote_unlock" in rows and "unlock_key" in rows
        assert validate is not None

    def test_off_is_the_default(self, hook):
        rows, _, _, _ = _services_rows(load_wizard(), hook=hook)
        assert rows["remote_unlock"].value is False

    def test_not_offered_on_a_plain_stick(self, hook):
        rows, _, shown, _ = _services_rows(load_wizard(), luks=None, hook=hook)
        assert "remote_unlock" not in rows
        assert shown is False, "with nothing else installed there is no Services screen"

    def test_not_offered_without_dropbear(self):
        # First boot has no network: a feature it cannot install is not offered.
        rows, _, _, _ = _services_rows(load_wizard(), hook=None)
        assert "remote_unlock" not in rows

    def test_the_key_row_has_room_for_an_rsa_key(self, hook):
        rows, _, _, _ = _services_rows(load_wizard(), hook=hook)
        assert rows["unlock_key"].text
        assert rows["unlock_key"].limit >= len(RSA_KEY)

    def test_the_help_says_it_is_ethernet_only(self, hook):
        rows, _, _, _ = _services_rows(load_wizard(), hook=hook)
        described, _ = rows["remote_unlock"].describe({"remote_unlock": True, "unlock_key": ""})
        assert "Wi-Fi" in described and "cable" in described
        assert str(templates.REMOTE_UNLOCK_PORT) in described

    def test_the_answers_are_kept(self, hook):
        _, _, _, state = _services_rows(load_wizard(), hook=hook, remote_unlock=True, unlock_key=f" {KEY} ")
        assert state.remote_unlock is True
        assert state.unlock_key == KEY


class TestTheKeyIsChecked:
    @pytest.fixture
    def check(self):
        return load_wizard().check_unlock_key

    def test_off_needs_nothing(self, check):
        assert check({"remote_unlock": False, "unlock_key": ""}, False) is None

    def test_on_without_a_key_is_refused_and_names_the_way_out(self, check):
        key, message = check({"remote_unlock": True, "unlock_key": ""}, True)
        assert key == "unlock_key"
        assert "portlin-remote-unlock" in message

    def test_a_public_key_line_is_accepted(self, check):
        assert check({"remote_unlock": True, "unlock_key": KEY}, False) is None
        assert check({"remote_unlock": True, "unlock_key": RSA_KEY}, False) is None

    def test_a_private_key_or_junk_is_not(self, check):
        for junk in ("-----BEGIN OPENSSH PRIVATE KEY-----", "sam@laptop", "ssh-ed25519", "AAAAC3Nz"):
            assert check({"remote_unlock": True, "unlock_key": junk}, False) is not None, junk

    def test_github_needs_the_network(self, check):
        assert check({"remote_unlock": True, "unlock_key": "github:sam"}, True) is None
        key, message = check({"remote_unlock": True, "unlock_key": "github:sam"}, False)
        assert "network" in message.lower()

    def test_a_bad_github_name_is_refused(self, check):
        assert check({"remote_unlock": True, "unlock_key": "github:"}, True) is not None
        assert check({"remote_unlock": True, "unlock_key": "github:-sam"}, True) is not None
        assert check({"remote_unlock": True, "unlock_key": "github:sam/repo"}, True) is not None


class TestTheWizardAppliesIt:
    @pytest.fixture
    def fb(self, tmp_path):
        fb = load_wizard()
        fb.REMOTE_UNLOCK_CONF = tmp_path / "remote-unlock.conf"
        fb.REMOTE_UNLOCK_KEYS = tmp_path / "authorized_keys"
        fb.REMOTE_UNLOCK_HOST_KEY = tmp_path / "host_key"
        fb.calls = []

        def fake_run(argv, *, check=True, stdin=None):
            fb.calls.append(argv)
            if argv[0] == fb.DROPBEARKEY:
                fb.REMOTE_UNLOCK_HOST_KEY.write_text("key")
            return subprocess.CompletedProcess(argv, 0, "", "")

        fb.run = fake_run
        return fb

    def test_on_writes_the_key_root_only_and_flips_the_switch(self, fb):
        fb.REMOTE_UNLOCK_HOST_KEY.write_text("made by write")
        fb.apply_remote_unlock(True, KEY)
        assert fb.REMOTE_UNLOCK_KEYS.read_text() == KEY + "\n"
        assert os.stat(fb.REMOTE_UNLOCK_KEYS).st_mode & 0o777 == 0o600
        assert "enabled=1\n" in fb.REMOTE_UNLOCK_CONF.read_text()
        assert f"port={templates.REMOTE_UNLOCK_PORT}\n" in fb.REMOTE_UNLOCK_CONF.read_text()
        assert not any(argv[0] == fb.DROPBEARKEY for argv in fb.calls), "the host key was there already"

    def test_a_stick_with_no_host_key_gets_one(self, fb):
        fb.apply_remote_unlock(True, KEY)
        assert any(argv[0] == fb.DROPBEARKEY and "ed25519" in argv for argv in fb.calls)
        assert fb.REMOTE_UNLOCK_HOST_KEY.exists()

    def test_off_flips_the_switch_and_keeps_the_keys_and_the_port(self, fb):
        fb.REMOTE_UNLOCK_KEYS.write_text(KEY + "\n")
        fb.REMOTE_UNLOCK_CONF.write_text("enabled=1\nport=2200\n")
        fb.apply_remote_unlock(False, "")
        text = fb.REMOTE_UNLOCK_CONF.read_text()
        assert "enabled=0\n" in text and "port=2200\n" in text
        assert fb.REMOTE_UNLOCK_KEYS.read_text() == KEY + "\n"

    def test_github_keeps_only_the_lines_that_are_keys(self, fb):
        def fake_run(argv, *, check=True, stdin=None):
            fb.calls.append(argv)
            if argv[0] == "curl":
                return subprocess.CompletedProcess(argv, 0, f"{KEY}\n<html>oops</html>\n{RSA_KEY}\n\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        fb.run = fake_run
        fb.REMOTE_UNLOCK_HOST_KEY.write_text("x")
        fb.apply_remote_unlock(True, "github:sam")
        curl = next(argv for argv in fb.calls if argv[0] == "curl")
        assert "https://github.com/sam.keys" in curl
        assert fb.REMOTE_UNLOCK_KEYS.read_text() == f"{KEY}\n{RSA_KEY}\n"

    def test_a_github_account_with_no_keys_does_not_switch_on(self, fb):
        fb.run = lambda argv, *, check=True, stdin=None: subprocess.CompletedProcess(argv, 0, "\n", "")
        with pytest.raises(RuntimeError):
            fb.apply_remote_unlock(True, "github:sam")
        assert not fb.REMOTE_UNLOCK_CONF.exists()
        assert not fb.REMOTE_UNLOCK_KEYS.exists()

    def test_a_failed_fetch_says_so(self, fb):
        fb.run = lambda argv, *, check=True, stdin=None: subprocess.CompletedProcess(
            argv, 22, "", "curl: (22) The requested URL returned error: 404")
        with pytest.raises(RuntimeError, match="404"):
            fb.apply_remote_unlock(True, "github:nobody")

    def test_it_is_applied_before_the_boot_files_and_cannot_stop_setup(self):
        # The Boot files task is the initramfs rebuild that carries it in, and
        # a GitHub that is down is not worth a stick with no account.
        source = WIZARD.read_text()
        body = source[source.index("def apply_all("):source.index("def apply_privilege")]
        assert body.index('task("Remote unlock"') < body.index('task("Boot files"')
        line = next(l for l in body.splitlines() if 'task("Remote unlock"' in l)
        assert line.rstrip().endswith(", True)")

    def test_the_summary_says_what_was_chosen(self, tmp_path):
        fb = load_wizard()
        hook = tmp_path / "dropbear"
        hook.write_text("")
        fb.DROPBEAR_HOOK = str(hook)
        off = dict((k, v) for k, _, v in fb.summary_groups(summary_state(fb, luks_device="/dev/sda4")))
        assert "remote unlock off" in off["services"]
        on = dict((k, v) for k, _, v in fb.summary_groups(
            summary_state(fb, luks_device="/dev/sda4", remote_unlock=True, unlock_key="github:sam")))
        assert "remote unlock on" in on["services"] and "GitHub sam" in on["services"]
        plain = dict((k, v) for k, _, v in fb.summary_groups(summary_state(fb, luks_device=None)))
        assert "remote unlock" not in plain.get("services", "")


class TestTheTool:
    @pytest.fixture
    def tool(self, tmp_path, monkeypatch):
        tool = _tool()
        monkeypatch.setattr(tool, "CONF", tmp_path / "remote-unlock.conf")
        monkeypatch.setattr(tool, "KEYS", tmp_path / "authorized_keys")
        monkeypatch.setattr(tool, "HOST_KEY", tmp_path / "host_key")
        return tool

    def test_a_key_line_a_file_and_junk(self, tool, tmp_path):
        assert tool.keys_from(KEY) == [KEY]
        keyfile = tmp_path / "id_ed25519.pub"
        keyfile.write_text(f"# comment\n{KEY}\n\n{RSA_KEY}\n")
        assert tool.keys_from(str(keyfile)) == [KEY, RSA_KEY]
        with pytest.raises(tool.Refusal):
            tool.keys_from("sam@laptop")
        with pytest.raises(tool.Refusal):
            tool.keys_from("github:not a name")

    def test_an_empty_file_is_refused_rather_than_switching_on_for_nobody(self, tool, tmp_path):
        empty = tmp_path / "empty.pub"
        empty.write_text("nothing here\n")
        with pytest.raises(tool.Refusal):
            tool.keys_from(str(empty))

    def test_the_conf_round_trips_and_defaults(self, tool):
        assert tool.read_conf() == {"enabled": False, "port": tool.DEFAULT_PORT}
        tool.write_conf(True, 2200)
        assert tool.read_conf() == {"enabled": True, "port": 2200}
        assert "enabled=1\n" in tool.CONF.read_text()

    def test_it_refuses_a_stick_whose_frozen_half_cannot_switch(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "root_source", lambda: "/dev/mapper/portlin_root")
        present = tmp_path / "present"
        present.write_text("")
        monkeypatch.setattr(tool, "DROPBEAR_HOOK", present)
        monkeypatch.setattr(tool, "GATE_HOOK", tmp_path / "absent")
        with pytest.raises(tool.Refusal, match="too old"):
            tool.require_stick()

    def test_it_refuses_a_plain_stick_and_a_stick_without_dropbear(self, tool, monkeypatch, tmp_path):
        monkeypatch.setattr(tool, "root_source", lambda: "/dev/sda4")
        with pytest.raises(tool.Refusal, match="not encrypted"):
            tool.require_stick()
        monkeypatch.setattr(tool, "root_source", lambda: "/dev/mapper/portlin_root")
        monkeypatch.setattr(tool, "DROPBEAR_HOOK", tmp_path / "absent")
        with pytest.raises(tool.Refusal, match="apt install dropbear-initramfs"):
            tool.require_stick()

    @pytest.fixture
    def ready(self, tool, monkeypatch, tmp_path):
        """A root shell on an encrypted stick with everything installed."""
        present = tmp_path / "present"
        present.write_text("")
        monkeypatch.setattr(tool, "root_source", lambda: "/dev/mapper/portlin_root")
        monkeypatch.setattr(tool, "DROPBEAR_HOOK", present)
        monkeypatch.setattr(tool, "GATE_HOOK", present)
        monkeypatch.setattr(tool.os, "geteuid", lambda: 0)
        tool.events = []
        monkeypatch.setattr(tool, "rebuild_initramfs", lambda: tool.events.append("rebuild"))
        monkeypatch.setattr(tool, "ensure_host_key", lambda: tool.events.append("hostkey"))
        monkeypatch.setattr(tool, "host_fingerprint", lambda: "SHA256:abc")
        return tool

    def test_on_writes_everything_then_rebuilds(self, ready, capsys):
        assert ready.main(["on", KEY]) == 0
        assert ready.keys_on_file() == [KEY]
        assert os.stat(ready.KEYS).st_mode & 0o777 == 0o600
        assert ready.read_conf()["enabled"] is True
        assert ready.events == ["hostkey", "rebuild"]
        out = capsys.readouterr().out
        assert f"ssh -p {ready.DEFAULT_PORT}" in out and "SHA256:abc" in out

    def test_on_with_nothing_to_answer_to_is_refused(self, ready, capsys):
        assert ready.main(["on"]) == 1
        assert ready.events == []
        assert "No key" in capsys.readouterr().err

    def test_add_key_keeps_what_was_there(self, ready):
        ready.main(["on", KEY])
        ready.main(["add-key", RSA_KEY])
        ready.main(["add-key", KEY])
        assert ready.keys_on_file() == [KEY, RSA_KEY]

    def test_off_keeps_the_keys_and_the_port(self, ready):
        ready.write_conf(True, 2200)
        ready.main(["on", KEY])
        assert ready.main(["off"]) == 0
        assert ready.read_conf() == {"enabled": False, "port": 2200}
        assert ready.keys_on_file() == [KEY]
        assert ready.events[-1] == "rebuild"

    def test_status_names_the_command_to_type(self, ready, monkeypatch, capsys):
        monkeypatch.setattr(ready, "addresses", lambda: ["192.168.1.42"])
        monkeypatch.setattr(ready, "key_label", lambda key: "label")
        ready.main(["on", KEY])
        capsys.readouterr()
        assert ready.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "remote unlock   on" in out
        assert f"ssh -p {ready.DEFAULT_PORT} root@192.168.1.42" in out
        assert "DHCP" in out

    def test_bad_usage_exits_2(self, tool, capsys):
        assert tool.main([]) == 2
        assert tool.main(["add-key"]) == 2
        assert "usage" in capsys.readouterr().err
