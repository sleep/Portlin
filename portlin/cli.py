"""Command line interface.

Everything destructive funnels through _confirm_target, which is deliberately
awkward: it makes you type the device path. A y/n prompt is answered reflexively,
and the cost of a reflex here is somebody's home directory.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import re
import sys
import tempfile
from pathlib import Path

from . import devices, install, preflight, rootfs
from .config import (
    DEFAULT_MIRROR,
    DEFAULT_SECURITY_MIRROR,
    DEFAULT_SUITE,
    BuildConfig,
    WriteConfig,
)
from .errors import AbortedError, PortlinError, TargetError
from .layout import format_size
from .packages import GROUPS, MINIMAL_GROUPS
from .runner import Runner

log = logging.getLogger("portlin")

SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?$", re.IGNORECASE)
SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(text: str) -> int:
    match = SIZE_RE.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"could not read '{text}' as a size. Try something like 32G."
        )
    value, unit = match.groups()
    return int(float(value) * SIZE_UNITS[unit.upper()])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portlin",
        description=(
            "Write a portable, persistent Debian + Xfce system to a USB stick "
            "that boots on any x86_64 machine, optionally with LUKS encryption."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every command")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be done without touching anything",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check whether this host can build and write sticks")
    sub.add_parser("devices", help="list block devices that could be targets")

    build = sub.add_parser("build", help="build a reusable rootfs tarball")
    _add_build_args(build)

    write = sub.add_parser("write", help="write an existing rootfs tarball to a target")
    _add_write_args(write)
    write.add_argument(
        "--rootfs", type=Path, required=True, help="rootfs tarball from 'portlin build'"
    )

    create = sub.add_parser("create", help="build and write in one step")
    _add_build_args(create, output_required=False)
    _add_write_args(create)
    create.add_argument(
        "--rootfs",
        type=Path,
        help="reuse this tarball if it exists, otherwise build it here",
    )

    package = sub.add_parser(
        "package", help="build portlin's own .deb packages without writing a stick"
    )
    package.add_argument(
        "--output", type=Path, required=True, help="directory to write the .deb files into"
    )
    package.add_argument(
        "--version", help="version to stamp onto the packages (default: local dev version)"
    )

    return parser


def _add_build_args(parser: argparse.ArgumentParser, *, output_required: bool = True) -> None:
    group = parser.add_argument_group("image contents")
    if output_required:
        group.add_argument("-o", "--output", type=Path, required=True, help="tarball to write")
    group.add_argument("--suite", default=DEFAULT_SUITE, help=f"Debian suite (default: {DEFAULT_SUITE})")
    group.add_argument("--mirror", default=DEFAULT_MIRROR)
    group.add_argument("--security-mirror", default=DEFAULT_SECURITY_MIRROR)
    group.add_argument(
        "--groups",
        help=f"comma-separated package groups (default: all). Known: {', '.join(GROUPS)}",
    )
    group.add_argument(
        "--minimal",
        action="store_true",
        help=f"install only {', '.join(MINIMAL_GROUPS)} (no desktop)",
    )
    group.add_argument("--extra", action="append", default=[], help="extra package (repeatable)")
    group.add_argument("--exclude", action="append", default=[], help="package to omit (repeatable)")
    group.add_argument("--work-dir", type=Path, help="build directory to use and keep")


def _add_write_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("target")
    group.add_argument("-t", "--target", required=True, help="block device or image file path")
    group.add_argument(
        "--image-size",
        type=parse_size,
        default=None,
        help="size for a new image file (default 8G). Deliberately small: the "
             "system grows to fill the stick on first boot, so a bigger image "
             "only means a slower flash",
    )
    group.add_argument("--encrypt", action="store_true", help="LUKS2-encrypt the root filesystem")
    group.add_argument(
        "--discard",
        action="store_true",
        help="allow TRIM through the LUKS layer (better for flash lifespan, "
             "reveals which blocks are unused)",
    )
    group.add_argument("--force", action="store_true", help="permit a non-removable target")
    group.add_argument("--yes", action="store_true", help="skip the confirmation prompt")


# --------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace, runner: Runner) -> int:
    checks = preflight.run_checks(need_build=True, need_write=True, need_encrypt=True)
    width = max(len(c.name) for c in checks)
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"[{mark}] {check.name.ljust(width)}  {check.detail}")
    return 0 if all(c.ok for c in checks) else 1


def cmd_devices(args: argparse.Namespace, runner: Runner) -> int:
    found = devices.list_devices(runner)
    if not found:
        print("no whole-disk block devices found")
        return 1
    for device in found:
        problems = devices.safety_problems(device)
        marker = " " if not problems else "!"
        print(f"{marker} {device.describe()}")
    if any(devices.safety_problems(d) for d in found):
        print("\n'!' marks devices portlin will refuse or question. Run with --force to override "
              "the removability check.")
    return 0


def _preflight(runner: Runner, **needs: bool) -> None:
    """Check the host, unless this is a dry run.

    A dry run executes nothing, so demanding root, Linux and a dozen packages
    would only stop people inspecting the plan from the machine they are
    developing on.
    """
    if runner.dry_run:
        return
    preflight.require(**needs)


def cmd_build(args: argparse.Namespace, runner: Runner) -> int:
    _preflight(runner, need_build=True)
    cfg = _build_config(args, args.output)
    path = rootfs.build_rootfs(cfg, runner)
    print(f"rootfs written to {path}")
    return 0


def cmd_write(args: argparse.Namespace, runner: Runner) -> int:
    _preflight(runner, need_write=True, need_encrypt=args.encrypt)
    cfg = _write_config(args, args.rootfs, runner)
    install.write_stick(cfg, runner)
    print(f"{args.target} is ready. Boot it and the first-boot wizard will finish setup.")
    return 0


def cmd_create(args: argparse.Namespace, runner: Runner) -> int:
    _preflight(runner, need_build=True, need_write=True, need_encrypt=args.encrypt)

    tarball = args.rootfs
    if tarball and tarball.exists():
        log.info("reusing existing rootfs %s", tarball)
    else:
        if tarball is None:
            tarball = Path(tempfile.gettempdir()) / f"portlin-{args.suite}-rootfs.tar.zst"
        rootfs.build_rootfs(_build_config(args, tarball), runner)

    cfg = _write_config(args, tarball, runner)
    install.write_stick(cfg, runner)
    print(f"{args.target} is ready. Boot it and the first-boot wizard will finish setup.")
    return 0


def cmd_package(args: argparse.Namespace, runner: Runner) -> int:
    """Build the runtime packages without writing a stick.

    Shared with the write path through portlin.package, so that a package
    published by CI and one built locally can never be assembled by two
    different code paths.
    """
    from . import package as pkg

    output = Path(args.output)
    version = args.version or pkg.local_version()
    for name in pkg.PACKAGES:
        root = output / name
        for relative, content in pkg.text_files(name).items():
            mode = 0o755 if relative in pkg.executable_paths(name) else 0o644
            content = content.replace(pkg.local_version(), version)
            runner.write_file(root / relative, content, mode=mode)
        for relative, source in pkg.binary_files(name).items():
            runner.copy_file(source, root / relative)
        runner.run(["dpkg-deb", "--build", str(root), str(output / f"{name}.deb")])
    print(f"packages written to {output}")
    return 0


# --------------------------------------------------------------------------

def _build_config(args: argparse.Namespace, output: Path) -> BuildConfig:
    if args.minimal and args.groups:
        raise PortlinError("--minimal and --groups cannot be combined")
    if args.minimal:
        groups = list(MINIMAL_GROUPS)
    elif args.groups:
        groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    else:
        groups = None

    return BuildConfig(
        output=output,
        suite=args.suite,
        mirror=args.mirror,
        security_mirror=args.security_mirror,
        groups=groups,
        extra_packages=args.extra,
        exclude_packages=args.exclude,
        work_dir=args.work_dir,
        keep_work_dir=args.work_dir is not None,
    )


def _write_config(args: argparse.Namespace, tarball: Path, runner: Runner) -> WriteConfig:
    _confirm_target(args, runner)
    passphrase = None
    if args.encrypt:
        # A dry run must not stop to interrogate someone about a passphrase it
        # will never use, but the plan still has to include the LUKS steps.
        passphrase = "dry-run-placeholder" if runner.dry_run else _prompt_passphrase()
    return WriteConfig(
        target=args.target,
        rootfs=tarball,
        encrypt=args.encrypt,
        passphrase=passphrase,
        discard=args.discard,
        force=args.force,
        assume_yes=args.yes,
        image_size=args.image_size,
    )


def _confirm_target(args: argparse.Namespace, runner: Runner) -> None:
    """Refuse unsafe targets, then make the user type the path to proceed."""
    target = args.target
    device = devices.find_device(runner, target) if target.startswith("/dev/") else None

    if device is not None:
        problems = devices.safety_problems(device, force=args.force)
        if problems:
            raise TargetError("refusing to write:\n" + "\n".join(f"  - {p}" for p in problems))
        summary = device.describe()
    else:
        summary = f"{target} (image file)"

    if args.yes or runner.dry_run:
        return
    if not sys.stdin.isatty():
        raise AbortedError("refusing to write without a confirmation. Pass --yes to proceed.")

    print(f"\nAbout to ERASE and rewrite:\n\n  {summary}\n")
    if device is not None and device.size_bytes:
        print(f"Everything on this {format_size(device.size_bytes)} device will be lost.\n")
    answer = input(f"Type the target path ({target}) to confirm, or anything else to abort: ")
    if answer.strip() != target:
        raise AbortedError("aborted, nothing was written")


def _prompt_passphrase() -> str:
    """Prompt twice, off the terminal echo, and never from a flag.

    A passphrase passed as an argument would be visible in the process list to
    every user on the machine and would land in shell history, so portlin has no
    flag for it at all.
    """
    while True:
        first = getpass.getpass("LUKS passphrase for the new stick: ")
        if len(first) < 8:
            print("Use at least 8 characters. This is the only thing between the stick and a stranger.")
            continue
        second = getpass.getpass("Repeat the passphrase: ")
        if first != second:
            print("Those did not match. Try again.")
            continue
        return first


COMMANDS = {
    "doctor": cmd_doctor,
    "devices": cmd_devices,
    "build": cmd_build,
    "write": cmd_write,
    "create": cmd_create,
    "package": cmd_package,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    runner = Runner(dry_run=args.dry_run)

    try:
        return COMMANDS[args.command](args, runner)
    except PortlinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if args.dry_run:
            print("\n--- dry run: commands that would have run ---")
            for line in runner.rendered():
                print(f"  {line}")
