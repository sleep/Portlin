"""Configuration objects for the two halves of portlin.

BuildConfig describes something reproducible: given the same values you get the
same rootfs. WriteConfig describes something destructive: it names a device that
is about to be erased.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import packages
from .errors import BuildError

DEFAULT_SUITE = "trixie"
DEFAULT_MIRROR = "http://deb.debian.org/debian"
DEFAULT_SECURITY_MIRROR = "http://security.debian.org/debian-security"

# non-free-firmware carries the wifi and GPU blobs that make an unknown laptop
# usable. Without it the stick boots but frequently has no network.
DEFAULT_COMPONENTS = "main contrib non-free-firmware"

DEFAULT_HOSTNAME = "portlin"
DEFAULT_LOCALE = "en_US.UTF-8"
DEFAULT_KEYMAP = "us"
DEFAULT_TIMEZONE = "UTC"


@dataclass
class BuildConfig:
    """Inputs to ``portlin build``."""

    output: Path
    suite: str = DEFAULT_SUITE
    mirror: str = DEFAULT_MIRROR
    security_mirror: str = DEFAULT_SECURITY_MIRROR
    components: str = DEFAULT_COMPONENTS
    groups: list[str] | None = None
    extra_packages: list[str] = field(default_factory=list)
    exclude_packages: list[str] = field(default_factory=list)
    work_dir: Path | None = None
    keep_work_dir: bool = False

    # Placeholder identity. The first-boot wizard replaces all of it; these
    # values only exist so the system is coherent if the wizard is skipped.
    hostname: str = DEFAULT_HOSTNAME
    locale: str = DEFAULT_LOCALE
    keymap: str = DEFAULT_KEYMAP
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        self.output = Path(self.output)
        if self.work_dir is not None:
            self.work_dir = Path(self.work_dir)
        if not self.suite.strip():
            raise BuildError("suite must not be empty")
        if "://" not in self.mirror:
            raise BuildError(f"mirror does not look like a URL: {self.mirror}")

    def package_list(self) -> list[str]:
        return packages.resolve(
            self.groups,
            extra=self.extra_packages,
            exclude=self.exclude_packages,
        )

    @property
    def component_list(self) -> list[str]:
        return self.components.split()


@dataclass
class WriteConfig:
    """Inputs to ``portlin write``.

    ``target`` is either a block device or a path to an image file. ``image_size``
    is required only when creating a new image file, and is meaningless for a
    real device.
    """

    target: str
    rootfs: Path
    encrypt: bool = False
    passphrase: str | None = None
    discard: bool = False
    force: bool = False
    assume_yes: bool = False
    image_size: int | None = None
    label: str = "portlin"

    def __post_init__(self) -> None:
        self.rootfs = Path(self.rootfs)
        if self.encrypt and not self.passphrase:
            raise BuildError("encryption requested but no passphrase was supplied")
        if not self.encrypt and self.passphrase:
            raise BuildError("a passphrase was supplied but encryption is not enabled")
