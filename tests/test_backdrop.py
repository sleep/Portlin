"""Checks on portlin-backdrop, which points the default backdrop at the
wallpaper that is true of the stick: root crimson only when it is encrypted.

Run against a temporary root standing in for /, with root_source stubbed,
so every case is a real symlink on a real filesystem.
"""

from __future__ import annotations

import os

import pytest

from conftest import load_tool
from portlin import package


@pytest.fixture
def backdrop():
    return load_tool("portlin-backdrop")


@pytest.fixture
def root(tmp_path):
    renders = tmp_path / "usr/share/backgrounds/portlin"
    renders.mkdir(parents=True)
    for variant in package.WALLPAPER_VARIANTS:
        (renders / f"{variant}-{package.DEFAULT_BACKDROP_SIZE}.png").write_bytes(b"\x89PNG")
    return tmp_path


def _link(root):
    return root / package.DEFAULT_BACKDROP


def test_an_encrypted_root_gets_the_crimson_render(backdrop, root, monkeypatch):
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/mapper/portlin_root")
    assert backdrop.main(root) == 0
    assert os.readlink(_link(root)) == "../portlin/portlin-1920x1080.png"
    assert _link(root).read_bytes() == b"\x89PNG"


def test_a_plain_root_gets_the_plain_render(backdrop, root, monkeypatch):
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/sdb4")
    backdrop.main(root)
    assert os.readlink(_link(root)) == "../portlin/portlin-plain-1920x1080.png"


def test_an_unknown_root_is_not_called_encrypted(backdrop, root, monkeypatch):
    # Understating protection is the smaller wrong than claiming it.
    monkeypatch.setattr(backdrop, "root_source", lambda: "")
    backdrop.main(root)
    assert "plain" in os.readlink(_link(root))


def test_it_follows_the_stick_when_it_is_encrypted_later(backdrop, root, monkeypatch):
    # portlin-encrypt arms the initramfs; the next boot is encrypted, and the
    # link written on the boot before has to change with it.
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/sdb4")
    backdrop.main(root)
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/mapper/portlin_root")
    backdrop.main(root)
    assert os.readlink(_link(root)) == "../portlin/portlin-1920x1080.png"
    assert not list(_link(root).parent.glob(".*portlin-new"))


def test_it_replaces_a_render_an_older_package_left_as_a_file(backdrop, root, monkeypatch):
    link = _link(root)
    link.parent.mkdir(parents=True)
    link.write_bytes(b"old copy")
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/sdb4")
    backdrop.main(root)
    assert link.is_symlink()


def test_a_correct_link_is_left_alone(backdrop, root, monkeypatch):
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/sdb4")
    backdrop.main(root)
    target = root / "usr/share/backgrounds/portlin/portlin-plain-1920x1080.png"
    assert backdrop.point(_link(root), target) is False


def test_without_renders_it_points_at_nothing(backdrop, tmp_path, monkeypatch):
    monkeypatch.setattr(backdrop, "root_source", lambda: "/dev/sdb4")
    assert backdrop.main(tmp_path) == 0
    assert not _link(tmp_path).is_symlink()


def test_it_agrees_with_the_package_about_paths_and_size(backdrop):
    assert str(backdrop.BACKDROP) == f"/{package.DEFAULT_BACKDROP}"
    assert backdrop.SIZE == package.DEFAULT_BACKDROP_SIZE
    for encrypted in (True, False):
        shipped = str(backdrop.render_for(encrypted)).lstrip("/")
        assert shipped in package.binary_files("portlin-desktop")
