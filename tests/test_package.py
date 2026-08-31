"""The package descriptions are pure data, so they are asserted directly."""

from __future__ import annotations

import pytest

from portlin import __version__, package


def test_local_builds_are_suffixed_so_they_sort_below_a_release():
    # A tilde sorts below the empty string in Debian version comparison, so
    # 0.1.0~local is older than 0.1.0 and the published build always wins.
    # Not asserted by comparing the strings here: Python orders them the other
    # way round, because the tilde rule is dpkg's, not Python's.
    assert package.local_version() == f"{__version__}~local"
    assert package.local_version().startswith(__version__)


def test_control_names_the_package_and_its_version():
    control = package.render_control(
        name="portlin-runtime",
        version="0.1.0~local",
        description="Portlin desktop integration and tools",
        depends=["python3"],
    )
    assert "Package: portlin-runtime" in control
    assert "Version: 0.1.0~local" in control
    assert "Architecture: all" in control
    assert "Depends: python3" in control
    assert control.endswith("\n")


def test_control_omits_recommends_when_there_are_none():
    control = package.render_control(
        name="portlin-archive-keyring",
        version="0.1.0~local",
        description="Portlin archive signing key",
        depends=[],
    )
    assert "Recommends:" not in control
    assert "Depends:" not in control


def test_runtime_recommends_rather_than_depends_on_wallpapers():
    # A --minimal stick has no desktop, so 14 MB of wallpaper must not be a
    # hard dependency of the tools.
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    assert "Recommends: portlin-wallpapers" in control
    assert "portlin-wallpapers" not in control.split("Depends:")[1].split("\n")[0]


def test_sources_entry_pins_the_architecture():
    # Without this apt requests binary-amd64/Packages, which this archive will
    # never publish, and reports a fetch failure on every apt update.
    entry = package.render_sources_entry()
    assert "Architectures: all" in entry
    assert "Signed-By: /usr/share/keyrings/portlin-archive-keyring.gpg" in entry


def test_keyring_package_ships_the_sources_entry():
    files = package.text_files("portlin-archive-keyring")
    assert "etc/apt/sources.list.d/portlin.sources" in files


def test_runtime_ships_the_three_tools_as_executables():
    executables = package.executable_paths("portlin-runtime")
    assert executables == {
        "usr/bin/portlin-info",
        "usr/bin/portlin-expand",
        "usr/bin/portlin-encrypt",
    }


@pytest.mark.parametrize("name", package.PACKAGES)
def test_every_package_has_a_control_file(name):
    assert "DEBIAN/control" in package.text_files(name)


def test_runtime_ships_the_shared_device_lookup_module():
    # Two of the three tools need to find the disk or partition backing the
    # root filesystem, and this is the one place that logic is written down.
    files = package.text_files("portlin-runtime")
    assert "usr/lib/portlin/devices.py" in files
    assert "usr/lib/portlin/devices.py" not in package.executable_paths("portlin-runtime")

    source = files["usr/lib/portlin/devices.py"]
    assert "def sysfs_node" in source
    assert "def backing_partition" in source
    # Regression guard: locate the device by its major:minor number, never by
    # assuming udev created a /sys/class/block/<name> symlink, because without
    # udev cryptsetup makes a real device node instead and that lookup finds
    # nothing for an encrypted root.
    assert "st_rdev" in source
    assert "/sys/class/block" not in source


def test_info_tool_is_shipped_and_executable():
    files = package.text_files("portlin-runtime")
    assert files["usr/bin/portlin-info"].startswith("#!/usr/bin/env python3")
    assert "usr/bin/portlin-info" in package.executable_paths("portlin-runtime")


def test_expand_tool_resizes_in_the_only_order_that_works():
    # Each layer can only grow into space the layer beneath it has claimed, so
    # the order is not a preference. Asserted on the source because the real
    # behaviour is covered by the harness against a live device.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-expand"]
    assert source.index("growpart") < source.index("cryptsetup")
    assert source.index("cryptsetup") < source.index("resize2fs")
