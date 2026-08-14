"""A uniform interface over the two things portlin can write to.

A real USB stick and a sparse image file behave identically once the image is
attached to a loop device, so the rest of the codebase never needs to know which
one it has. That single abstraction is what makes it possible to develop and
test the write path on a machine with no USB stick in it, or no USB at all.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from .errors import TargetError
from .layout import partition_path
from .runner import Runner


class Target(ABC):
    """Something with a device path and numbered partitions."""

    @property
    @abstractmethod
    def device(self) -> str:
        """Path of the whole-disk device node."""

    @abstractmethod
    def size_bytes(self) -> int: ...

    def partition(self, number: int) -> str:
        return partition_path(self.device, number)

    def open(self) -> "Target":
        return self

    def close(self) -> None:
        return None

    def __enter__(self) -> "Target":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()


class DeviceTarget(Target):
    """A real block device."""

    def __init__(self, path: str, runner: Runner) -> None:
        self._path = path
        self._runner = runner

    @property
    def device(self) -> str:
        return self._path

    def size_bytes(self) -> int:
        raw = self._runner.output(
            ["blockdev", "--getsize64", self._path],
            dry_stdout=str(32 * 1024**3),
        )
        try:
            return int(raw)
        except ValueError as exc:
            raise TargetError(
                f"could not determine the size of {self._path}: {raw!r}"
            ) from exc


class ImageTarget(Target):
    """A sparse image file, attached to a loop device while open.

    ``-P`` asks the kernel to scan the image's partition table and create
    /dev/loopNpM nodes, which is what lets the same install code path work
    unmodified against a file.
    """

    def __init__(
        self,
        path: str | Path,
        runner: Runner,
        *,
        size_bytes: int | None = None,
    ) -> None:
        self.path = Path(path)
        self._runner = runner
        self._requested_size = size_bytes
        self._loop: str | None = None

    @property
    def device(self) -> str:
        if self._loop is None:
            raise TargetError("image target is not attached to a loop device")
        return self._loop

    def size_bytes(self) -> int:
        if self.path.exists():
            return self.path.stat().st_size
        # The file has not been allocated yet, which is the normal state during a
        # dry run. Fall back to what the caller asked for.
        if self._requested_size is not None:
            return self._requested_size
        raise TargetError(f"{self.path} does not exist and no size was requested")

    def open(self) -> "ImageTarget":
        if self._requested_size is not None:
            if self.path.exists() and self.path.stat().st_size != self._requested_size:
                raise TargetError(
                    f"{self.path} already exists with a different size. "
                    f"Remove it or omit --image-size."
                )
            self._allocate(self._requested_size)
        elif not self.path.exists():
            # A new image with no size given gets the default. The image is
            # meant to be small: first-boot expansion claims the rest of
            # whatever stick it is written to.
            from .layout import DEFAULT_IMAGE_BYTES

            self._requested_size = DEFAULT_IMAGE_BYTES
            self._allocate(DEFAULT_IMAGE_BYTES)

        self._loop = self._runner.output(
            ["losetup", "-P", "-f", "--show", str(self.path)],
            dry_stdout="/dev/loop0",
        )
        if not self._loop:
            raise TargetError(f"losetup did not report a device for {self.path}")
        return self

    def close(self) -> None:
        if self._loop is None:
            return
        # Best effort: a failure here must not mask the original exception that
        # triggered the unwind.
        self._runner.run(["losetup", "-d", self._loop], check=False)
        self._loop = None

    def _allocate(self, size_bytes: int) -> None:
        self._runner.commands.append(["allocate", str(self.path), str(size_bytes)])
        if self._runner.dry_run:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.truncate(size_bytes)


def open_target(
    spec: str, runner: Runner, *, image_size: int | None = None
) -> Target:
    """Build the right Target for ``spec``.

    A path under /dev that is a block device is treated as a device; anything
    else is treated as an image file. Being explicit about the block-device test
    avoids the trap where a typo like /dev/sdx1x silently creates a regular file
    named after a device.
    """
    path = Path(spec)
    if path.exists() and _is_block_device(path):
        if image_size is not None:
            raise TargetError("--image-size is meaningless for a real block device")
        return DeviceTarget(str(path), runner)
    if spec.startswith("/dev/"):
        raise TargetError(f"{spec} is not an existing block device")
    return ImageTarget(path, runner, size_bytes=image_size)


def _is_block_device(path: Path) -> bool:
    try:
        import stat

        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False
