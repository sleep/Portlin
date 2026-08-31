"""The package descriptions are pure data, so they are asserted directly."""

from __future__ import annotations

from pathlib import Path

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


def test_runtime_recommends_rather_than_depends_on_desktop():
    # A --minimal stick has no desktop, so the theme and 14 MB of wallpaper
    # must not be a hard dependency of the tools.
    control = package.text_files("portlin-runtime")["DEBIAN/control"]
    assert "Recommends: portlin-desktop" in control
    assert "portlin-desktop" not in control.split("Depends:")[1].split("\n")[0]


def test_sources_entry_pins_the_architecture():
    # Without this apt requests binary-amd64/Packages, which this archive will
    # never publish, and reports a fetch failure on every apt update.
    entry = package.render_sources_entry()
    assert "Architectures: all" in entry
    assert "Signed-By: /usr/share/keyrings/portlin-archive-keyring.gpg" in entry


def test_keyring_package_ships_no_key_or_source_while_the_placeholder_is_empty():
    # The repository ships a zero-byte placeholder until a real signing key is
    # committed. Shipping the apt source without it would point every stick at
    # an archive that either does not exist yet or cannot be verified, failing
    # apt update on every run, forever.
    assert package.KEYRING_FILE.stat().st_size == 0
    assert "etc/apt/sources.list.d/portlin.sources" not in package.text_files(
        "portlin-archive-keyring"
    )
    assert package.binary_files("portlin-archive-keyring") == {}


def test_keyring_package_ships_the_key_and_source_once_a_real_key_lands(
    tmp_path, monkeypatch
):
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    files = package.text_files("portlin-archive-keyring")
    assert "etc/apt/sources.list.d/portlin.sources" in files
    assert package.binary_files("portlin-archive-keyring") == {
        package.KEYRING_PATH.lstrip("/"): real_key
    }


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


@pytest.mark.parametrize("name", package.PACKAGES)
def test_text_files_defaults_to_the_local_version(name):
    control = package.text_files(name)["DEBIAN/control"]
    assert f"Version: {package.local_version()}" in control


@pytest.mark.parametrize("name", package.PACKAGES)
def test_text_files_stamps_the_requested_version_onto_control(name):
    # A CI release build stamps a real version onto content this module still
    # renders with the ~local suffix by default, so the version has to be
    # threaded into render_control rather than patched into rendered text.
    control = package.text_files(name, version="2.5.1")["DEBIAN/control"]
    assert "Version: 2.5.1" in control
    assert package.local_version() not in control


@pytest.mark.parametrize("name", package.PACKAGES)
def test_requested_version_touches_only_the_control_file(name):
    # Nothing else in a package's tree names a version, so a version string
    # must never show up anywhere but DEBIAN/control.
    files = package.text_files(name, version="2.5.1")
    for relative, content in files.items():
        if relative != "DEBIAN/control":
            assert "2.5.1" not in content, relative


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


def test_encrypt_tool_refuses_without_the_frozen_finaliser():
    # The frozen tier of a stick written before the finaliser existed cannot be
    # brought forward, so the tool must detect its absence rather than arm an
    # encryption nothing on that stick can complete.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "/usr/local/sbin/portlin-finalise-encryption" in source


def test_encrypt_tool_does_not_encrypt_anything_itself():
    # All the dangerous work stays in the frozen, harness-tested initramfs
    # script. This tool only sets the flag that wakes it up.
    source = package.text_files("portlin-runtime")["usr/bin/portlin-encrypt"]
    assert "reencrypt" not in source
    assert "luksFormat" not in source
    assert "portlin.encrypt=ask" in source


def test_pkname_lookups_exclude_holder_devices():
    # Without -d (no-deps), lsblk lists the whole subtree rooted at the queried
    # device -- including an open LUKS mapping sitting on top of the very
    # partition being asked about, which is the ordinary case for a live,
    # mounted, encrypted stick. That returns two rows glued into one string
    # instead of one, and the harness caught this reaching a real growpart
    # call with an empty partition number.
    files = package.text_files("portlin-runtime")
    for tool in ("usr/bin/portlin-info", "usr/bin/portlin-expand"):
        source = files[tool]
        assert '"lsblk", "-no", "PKNAME"' not in source
        assert '"lsblk", "-dno", "PKNAME"' in source


def test_tools_use_the_shared_device_module_not_a_hand_copy():
    # A hand-written /sys/class/block lookup has already reached a USB stick
    # once in this project's history: it resolves the mapper alias for an
    # encrypted root, which sysfs never created because cryptsetup made a real
    # device node there instead of a udev symlink. Every tool must import the
    # shared lookup in usr/lib/portlin/devices.py rather than repeat that bug,
    # or duplicate the command-running and root-finding helpers that live
    # there alongside it.
    files = package.text_files("portlin-runtime")
    for tool in ("usr/bin/portlin-info", "usr/bin/portlin-expand", "usr/bin/portlin-encrypt"):
        source = files[tool]
        assert "/sys/class/block" not in source
        assert "/usr/lib/portlin" in source
        assert "from devices import" in source


def test_desktop_ships_every_theme_file():
    files = package.text_files("portlin-desktop")
    for destination in package.THEME_FILES:
        assert destination in files, destination
    assert "Greybird-dark" in files["etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml"]


def test_theme_files_are_not_executable():
    assert not (package.executable_paths("portlin-desktop")
                & set(package.THEME_FILES))


def test_runtime_carries_no_desktop_configuration():
    # A --minimal stick installs portlin-runtime alone. Theme configuration
    # for lightdm or xfconf in that package would misrepresent software the
    # stick does not have, so it belongs only in portlin-desktop.
    files = package.text_files("portlin-runtime")
    assert not any(path.startswith("etc/xdg/") for path in files)
    assert not any(path.startswith("etc/lightdm/") for path in files)


def test_every_declared_binary_member_exists():
    for name in package.PACKAGES:
        for destination, source in package.binary_files(name).items():
            assert source.exists(), f"{source} is missing for {destination}"


def test_wallpapers_carry_every_declared_size():
    destinations = package.binary_files("portlin-desktop")
    assert len(destinations) == len(package.WALLPAPER_SIZES)
    assert all(d.startswith("usr/share/backgrounds/portlin/") for d in destinations)


def test_desktop_declares_exactly_its_seven_etc_paths_as_conffiles():
    # Without this member dpkg treats these /etc files as ordinary package
    # files and overwrites a locally-edited one silently on every upgrade.
    files = package.text_files("portlin-desktop")
    conffiles = files["DEBIAN/conffiles"].splitlines()
    assert len(conffiles) == 7
    assert set(conffiles) == {f"/{destination}" for destination in package.THEME_FILES}


def test_keyring_declares_its_sources_entry_as_a_conffile(tmp_path, monkeypatch):
    # A user who points portlin.sources at a mirror must not lose that edit
    # to the next apt update of the keyring package. Only assertable once a
    # real key ships the sources entry at all.
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    files = package.text_files("portlin-archive-keyring")
    assert files["DEBIAN/conffiles"] == "/etc/apt/sources.list.d/portlin.sources\n"


def test_runtime_ships_no_etc_files_and_declares_no_conffiles():
    # portlin-runtime carries no /etc files (test_runtime_carries_no_desktop_
    # configuration), so a DEBIAN/conffiles member here would either be empty
    # or, worse, list paths the package does not actually ship.
    files = package.text_files("portlin-runtime")
    assert "DEBIAN/conffiles" not in files


def test_conffiles_also_cover_binary_members_under_etc(monkeypatch):
    # A binary member under /etc must be declared just like a text one, or
    # dpkg treats it as an ordinary file and silently overwrites a local edit
    # on every upgrade. portlin-runtime ships no /etc files of its own, which
    # makes it a clean package to prove this on: without this, the fake
    # binary member below would produce no DEBIAN/conffiles member at all.
    monkeypatch.setattr(
        package,
        "binary_files",
        lambda name: {"etc/portlin/example.bin": Path("/dev/null")},
    )
    files = package.text_files("portlin-runtime")
    assert files["DEBIAN/conffiles"] == "/etc/portlin/example.bin\n"


def test_keyring_package_carries_the_key_at_the_path_the_source_names(
    tmp_path, monkeypatch
):
    # The Signed-By path in the apt source and the path the package installs
    # the key to are the same string in two files, and apt fails silently on
    # every update if they drift apart.
    real_key = tmp_path / "portlin-archive-keyring.gpg"
    real_key.write_bytes(b"not a real key, just non-empty")
    monkeypatch.setattr(package, "KEYRING_FILE", real_key)

    destinations = package.binary_files("portlin-archive-keyring")
    assert package.KEYRING_PATH.lstrip("/") in destinations
    assert package.KEYRING_PATH in package.render_sources_entry()
