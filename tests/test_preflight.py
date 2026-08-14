from __future__ import annotations

import pytest

from portlin import preflight
from portlin.errors import PreflightError


class TestArchitectureCheck:
    def test_x86_64_passes(self, monkeypatch):
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        assert preflight.check_arch().ok

    def test_arm64_fails_with_an_explanation(self, monkeypatch):
        # Building a Debian amd64 system means running amd64 maintainer scripts,
        # so an arm64 host cannot do it without emulation portlin does not manage.
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        check = preflight.check_arch()
        assert not check.ok
        assert "x86_64" in check.detail


class TestKernelCheck:
    def test_linux_passes(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        assert preflight.check_linux().ok

    def test_darwin_fails(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        check = preflight.check_linux()
        assert not check.ok
        assert "Darwin" in check.detail


class TestRootCheck:
    def test_uid_zero_passes(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 0)
        assert preflight.check_root().ok

    def test_a_normal_user_is_told_to_use_sudo(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        assert "sudo" in preflight.check_root().detail


class TestToolChecks:
    def test_all_present_passes(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert preflight.check_tools(preflight.WRITE_TOOLS, "write tools").ok

    def test_a_missing_tool_names_the_package_to_install(self, monkeypatch):
        monkeypatch.setattr(
            "shutil.which", lambda name: None if name == "sgdisk" else f"/usr/bin/{name}"
        )
        check = preflight.check_tools(preflight.WRITE_TOOLS, "write tools")
        assert not check.ok
        assert "sgdisk" in check.detail
        assert "apt install gdisk" in check.detail

    def test_multiple_missing_tools_are_reported_together(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        check = preflight.check_tools(preflight.WRITE_TOOLS, "write tools")
        assert "gdisk" in check.detail
        assert "e2fsprogs" in check.detail


class TestOptionalTools:
    def test_udevadm_is_optional_not_required(self, monkeypatch):
        # install.py calls udevadm with check=False on purpose. A hard
        # requirement here would contradict that and lock minimal or
        # containerised hosts out of writing a stick.
        assert "udevadm" not in preflight.WRITE_TOOLS
        assert "udevadm" in preflight.OPTIONAL_TOOLS

    def test_a_missing_optional_tool_still_reports_ok(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        check = preflight.check_optional_tools()
        assert check.ok
        assert "udevadm" in check.detail
        assert "not required" in check.detail

    def test_write_succeeds_without_udevadm(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr(
            "shutil.which",
            lambda name: None if name == "udevadm" else f"/usr/bin/{name}",
        )
        preflight.require(need_write=True, need_encrypt=True)


class TestRequire:
    def test_reports_every_failure_at_once(self, monkeypatch):
        # A user missing three things should learn that in one run, not discover
        # them one twenty-minute build at a time.
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        monkeypatch.setattr("os.geteuid", lambda: 1000)
        with pytest.raises(PreflightError) as exc:
            preflight.require(need_write=True)
        message = str(exc.value)
        assert "kernel" in message
        assert "architecture" in message
        assert "root" in message

    def test_passes_on_a_capable_host(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        preflight.require(need_build=True, need_write=True, need_encrypt=True)

    def test_encryption_tools_are_only_required_when_encrypting(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        monkeypatch.setattr("os.geteuid", lambda: 0)
        monkeypatch.setattr(
            "shutil.which",
            lambda name: None if name == "cryptsetup" else f"/usr/bin/{name}",
        )
        preflight.require(need_write=True)
        with pytest.raises(PreflightError, match="cryptsetup"):
            preflight.require(need_write=True, need_encrypt=True)
