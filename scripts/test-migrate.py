#!/usr/bin/env python3
"""Migrate from a real second stick, encrypted or not, and from an archive.

The unit tests prove portlin-migrate plans the right rsync and tar command
lines. What only a real device shows is whether those lines do what the plan
says: that rsync's --backup-dir really moves a replaced file aside, that a
merged directory keeps its local-only file, that the account comes back with
the same uid and password hash, that the source is untouched and its LUKS
mapping is gone afterwards, and that an archive written by export restores
through the same checks.

    python3 scripts/test-migrate.py [--encrypt]
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = "/usr/bin/portlin-migrate"
PASSPHRASE = "migrate-harness-passphrase"
DISK = Path("/tmp/portlin-migrate-test.img")
DISK_SIZE = 4 * 1024**3
MOUNT = Path("/mnt/old-stick")
MAPPING = "old_portlin_root"
USER = "olduser"
UID = 1500
PASSWORD = "old-password"
failures: list[str] = []


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, capture_output=True, text=True, **kwargs)


def must(argv: list[str], **kwargs) -> str:
    result = run(argv, **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"setup failed: {' '.join(argv)}\n{result.stderr}")
    return result.stdout.strip()


def ok(message: str) -> None:
    print(f"ok: {message}", flush=True)


def bad(message: str) -> None:
    print(f"FAIL: {message}", flush=True)
    failures.append(message)


def check(condition: bool, message: str) -> None:
    ok(message) if condition else bad(message)


def ensure_node(path: str) -> None:
    name = Path(path).name
    numbers = Path("/sys/class/block") / name / "dev"
    if not numbers.exists():
        raise SystemExit(f"kernel does not know about {path}")
    major, minor = numbers.read_text().strip().split(":")
    if Path(path).exists():
        os.unlink(path)
    must(["mknod", "-m", "0660", path, "b", major, minor])


def install_portlin_packages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / "packages"
        built.mkdir()
        result = run([sys.executable, "-m", "portlin", "package", "--output", str(built)], cwd=REPO)
        if result.returncode != 0:
            sys.exit(f"building portlin's packages failed:\n{result.stderr}")
        debs = sorted(str(deb) for deb in built.glob("*.deb"))
        result = run(["apt-get", "install", "-y", "-q", *debs],
                     env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"})
        if result.returncode != 0:
            sys.exit(f"installing portlin's packages failed:\n{result.stdout}")
    check(Path(TOOL).exists(), f"{TOOL} is installed")


def make_old_stick(encrypt: bool) -> tuple[str, str]:
    """A second portlin, as write and first boot would leave it: labels, a
    release file, one account, a home, a wifi password, a software record."""
    DISK.unlink(missing_ok=True)
    with open(DISK, "wb") as handle:
        handle.truncate(DISK_SIZE)
    must(["sgdisk", "-n1:0:+1M", "-t1:EF02", "-n2:0:+64M", "-t2:EF00",
          "-n3:0:+128M", "-t3:8300", "-n4:0:0", "-t4:8300", str(DISK)])
    loop = must(["losetup", "-P", "-f", "--show", str(DISK)])
    boot, root = f"{loop}p3", f"{loop}p4"
    ensure_node(boot)
    ensure_node(root)
    must(["mkfs.ext4", "-q", "-F", "-L", "portlin-boot", boot])
    device = root
    if encrypt:
        formatted = subprocess.run(
            ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--pbkdf", "argon2id",
             "--pbkdf-memory", "32768", "--key-file", "-", root],
            input=PASSPHRASE, capture_output=True, text=True)
        if formatted.returncode != 0:
            raise SystemExit(f"luksFormat failed: {formatted.stderr}")
        opened = subprocess.run(["cryptsetup", "open", "--key-file", "-", root, MAPPING],
                                input=PASSPHRASE, capture_output=True, text=True)
        if opened.returncode != 0:
            raise SystemExit(f"open failed: {opened.stderr}")
        device = f"/dev/mapper/{MAPPING}"
    must(["mkfs.ext4", "-q", "-F", "-L", "portlin-root", device])
    MOUNT.mkdir(parents=True, exist_ok=True)
    must(["mount", device, str(MOUNT)])

    password_hash = must(["openssl", "passwd", "-6", "-salt", "saltsalt", PASSWORD])
    (MOUNT / "etc").mkdir()
    (MOUNT / "etc/portlin-release").write_text("PORTLIN_VERSION=0.0.9\nPORTLIN_URL=x\n")
    (MOUNT / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        f"{USER}:x:{UID}:{UID}:Old User,,,:/home/{USER}:/bin/bash\n")
    (MOUNT / "etc/shadow").write_text(f"root:*:19000:0:99999:7:::\n{USER}:{password_hash}:19000:0:99999:7:::\n")
    (MOUNT / "etc/group").write_text(f"root:x:0:\nsudo:x:27:{USER}\naudio:x:29:{USER}\n{USER}:x:{UID}:\n")
    (MOUNT / "etc/sudoers.d").mkdir()
    (MOUNT / "etc/sudoers.d/50-portlin-nopasswd").write_text(f"{USER} ALL=(ALL) NOPASSWD: ALL\n")
    (MOUNT / "etc/hostname").write_text("oldbox\n")
    connections = MOUNT / "etc/NetworkManager/system-connections"
    connections.mkdir(parents=True)
    (connections / "cafe.nmconnection").write_text("[wifi-security]\npsk=secret\n")
    (connections / "cafe.nmconnection").chmod(0o600)
    state = MOUNT / "var/lib/portlin/software"
    state.mkdir(parents=True)
    (state / "mullvad.json").write_text("{}\n")
    home = MOUNT / "home" / USER
    (home / "Documents").mkdir(parents=True)
    (home / "Documents/canary.txt").write_text("from the old stick\n")
    (home / "Downloads").mkdir()
    (home / "Downloads/big.bin").write_bytes(os.urandom(1024 * 1024))
    (home / ".config/app").mkdir(parents=True)
    (home / ".config/app/settings.ini").write_text("[app]\ncolour=blue\n")
    (home / ".cache").mkdir()
    (home / ".cache/junk").write_text("junk\n")
    (home / "link-to-docs").symlink_to("Documents")
    must(["chown", "-R", f"{UID}:{UID}", str(home)])
    must(["umount", str(MOUNT)])
    if encrypt:
        must(["cryptsetup", "close", MAPPING])
    return loop, root


def clear_local_accounts() -> None:
    """portlin-migrate's local_target() lands the migrated account's files in
    whichever uid 1000+ account it finds first when nothing asked for a
    specific one, exactly the range read_accounts() offers from a source.
    test-software.py, which runs earlier in the same container, leaves its
    "scripted" account behind; left alone it would silently steer olduser's
    files into that account instead of a freshly created olduser."""
    for line in must(["getent", "passwd"]).splitlines():
        fields = line.split(":")
        if len(fields) < 3 or not fields[2].isdigit():
            continue
        name, uid = fields[0], int(fields[2])
        if uid >= 1000 and uid != 65534 and name != USER:
            run(["userdel", "-r", name])


def shadow_hash(user: str) -> str:
    for line in Path("/etc/shadow").read_text().splitlines():
        fields = line.split(":")
        if fields[0] == user:
            return fields[1]
    return ""


def tool(*verb: str, stdin: str = "") -> subprocess.CompletedProcess:
    return run([TOOL, *verb], input=stdin)


def write_plan(path: Path, *, source: str, kind: str, encrypted: bool, ids: list[str], inventory: dict) -> None:
    path.write_text(json.dumps({
        "source": source, "kind": kind, "encrypted": encrypted, "ids": ids,
        "firstboot": False, "label": "harness", "inventory": inventory,
    }))


def source_mtime(root: str, encrypt: bool) -> float:
    device = root
    if encrypt:
        subprocess.run(["cryptsetup", "open", "--readonly", "--key-file", "-", root, MAPPING],
                       input=PASSPHRASE, capture_output=True, text=True)
        device = f"/dev/mapper/{MAPPING}"
    must(["mount", "-o", "ro,noload", device, str(MOUNT)])
    try:
        return (MOUNT / "home" / USER / "Documents/canary.txt").stat().st_mtime
    finally:
        must(["umount", str(MOUNT)])
        if encrypt:
            must(["cryptsetup", "close", MAPPING])


def check_stick_to_stick(root: str, encrypt: bool) -> dict:
    stdin = PASSPHRASE + "\n" if encrypt else ""
    listed = tool("candidates", "--json")
    candidates = json.loads(listed.stdout or "[]")
    found = any(c["path"] == root and c["encrypted"] == encrypt for c in candidates)
    if not found:
        # candidates comes from lsblk by way of portlin-migrate's own parsing,
        # so a failure here could be either side: print both raw outputs so
        # the next run does not need to be reproduced by hand to tell which.
        print(f"candidates --json said: {listed.stdout!r}", flush=True)
        raw = run(["lsblk", "--json", "-o", "NAME,PATH,LABEL,FSTYPE,SIZE,MODEL,TRAN,PARTN"])
        print(f"raw lsblk said: {raw.stdout!r}", flush=True)
    check(found, f"candidates lists {root} (encrypted={encrypt})")

    before = source_mtime(root, encrypt)

    read = tool("inventory", root, "--json", stdin=stdin)
    check(read.returncode == 0, "inventory reads the old stick")
    inventory = json.loads(read.stdout)
    ids = {item["id"]: item for item in inventory["items"]}
    check(f"account.{USER}" in ids, "the old account is offered")
    check(ids["home.files.Documents"]["bytes"] > 0, "home entries carry sizes")
    check(ids["home.settings..cache"]["default"] is False, ".cache is offered but off")
    check("software.mullvad" in ids, "the software record is offered")

    plan = Path("/tmp/migrate-plan.json")
    chosen = [f"account.{USER}", "identity.hostname", "home.files.Documents", "home.files.Downloads",
              "home.files.link-to-docs", "home.settings..config", "network"]
    write_plan(plan, source=root, kind="stick", encrypted=encrypt, ids=chosen, inventory=inventory)

    applied = tool("apply", "--plan", str(plan), stdin=stdin)
    print(applied.stdout)
    check(applied.returncode == 0, "apply exits 0")
    check("::result ok" in applied.stdout, "apply reports ok")
    check(any(line.startswith("::progress 100") for line in applied.stdout.splitlines()), "progress reaches 100")

    record = pwd.getpwnam(USER)
    check(record.pw_uid == UID, f"the account was recreated with uid {UID}")
    check(shadow_hash(USER) == must(["openssl", "passwd", "-6", "-salt", "saltsalt", PASSWORD]),
          "the password hash came across")
    check(Path("/etc/hostname").read_text().strip() == "oldbox", "the hostname was applied")
    home = Path(record.pw_dir)
    canary = home / "Documents/canary.txt"
    check(canary.read_text() == "from the old stick\n", "the canary arrived")
    check(canary.stat().st_uid == UID, "files belong to the local account")
    check((home / "link-to-docs").is_symlink(), "a symlink is still a symlink")
    check((home / ".config/app/settings.ini").exists(), "dot directories arrived")
    check(not (home / ".cache").exists(), "the unticked .cache did not")
    connection = Path("/etc/NetworkManager/system-connections/cafe.nmconnection")
    check(connection.exists() and stat.S_IMODE(connection.stat().st_mode) == 0o600 and connection.stat().st_uid == 0,
          "the wifi connection is root-owned and 0600")
    check(not (home / ".portlin-migrate-backup").exists(), "nothing was moved aside on a fresh home")

    check(run(["findmnt", "/run/portlin/migrate/source"]).returncode != 0, "the source is unmounted afterwards")
    check("portlin-migrate-source" not in run(["dmsetup", "ls"]).stdout, "the mapping is closed afterwards")
    check(source_mtime(root, encrypt) == before, "the source was not touched")

    print("== second run: collision, merge, idempotence ==", flush=True)
    canary.write_text("local edit\n")
    (home / "Documents/only-here.txt").write_text("keep me\n")
    time.sleep(1.1)
    applied = tool("apply", "--plan", str(plan), stdin=stdin)
    check(applied.returncode == 0, "a second apply exits 0")
    check(canary.read_text() == "from the old stick\n", "the old stick's file wins")
    check((home / "Documents/only-here.txt").read_text() == "keep me\n", "a local-only file survives the merge")
    backups = sorted((home / ".portlin-migrate-backup").glob("*/Documents/canary.txt"))
    check(bool(backups) and backups[-1].read_text() == "local edit\n", "the replaced file is in the backup tree")
    check("Files that were replaced are under" in applied.stdout, "the summary names the backup tree")
    return inventory


def check_archive_round_trip() -> None:
    print("== export and restore from the archive ==", flush=True)
    folder = Path("/tmp/migrate-backups")
    folder.mkdir(exist_ok=True)
    exported = tool("export", str(folder))
    print(exported.stdout)
    check(exported.returncode == 0, "export exits 0")
    archives = sorted(folder.glob("*.portlin-backup.tar.zst"))
    check(bool(archives), "an archive was written")
    if not archives:
        return
    archive = archives[-1]
    check(stat.S_IMODE(archive.stat().st_mode) == 0o600, "the archive is 0600")
    listing = must(["tar", "-I", "zstd", "-tf", str(archive)]).splitlines()
    check(listing[0] == "manifest.json", "the manifest is member 0")

    read = tool("inventory", str(archive), "--json")
    check(read.returncode == 0, "inventory reads the archive")
    inventory = json.loads(read.stdout)
    home = Path(pwd.getpwnam(USER).pw_dir)
    canary = home / "Documents/canary.txt"
    canary.write_text("clobbered\n")
    plan = Path("/tmp/migrate-archive-plan.json")
    write_plan(plan, source=str(archive), kind="archive", encrypted=False,
               ids=["home.files.Documents"], inventory=inventory)
    applied = tool("apply", "--plan", str(plan))
    print(applied.stdout)
    check(applied.returncode == 0, "apply from the archive exits 0")
    check(canary.read_text() == "from the old stick\n", "the canary is back from the archive")
    check(canary.stat().st_uid == UID, "restored files belong to the local account")
    backups = sorted((home / ".portlin-migrate-backup").glob("*/Documents/canary.txt"))
    check(bool(backups) and backups[-1].read_text() == "clobbered\n", "the clobbered file was moved aside")


DISPLAY = ":94"
WINDOW = "/usr/bin/portlin-migration"


def start_xvfb() -> subprocess.Popen:
    server = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1024x768x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = DISPLAY
    for _ in range(50):
        if subprocess.run(["xset", "q"], capture_output=True).returncode == 0:
            return server
        time.sleep(0.2)
    raise SystemExit("Xvfb never came up")


def check_the_window(inventory: dict) -> None:
    """Build the real window against a real X server and feed it what the
    tool printed, without a second pkexec: the answers are handed to the
    same handlers the Job would call."""
    import importlib.machinery
    import importlib.util

    server = start_xvfb()
    try:
        sys.path.insert(0, "/usr/lib/portlin")
        loader = importlib.machinery.SourceFileLoader("portlin_migration", WINDOW)
        spec = importlib.util.spec_from_file_location(loader.name, WINDOW, loader=loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = module
        spec.loader.exec_module(module)

        # The constructor asks the tool for candidates; answer it by hand so
        # the window is built without a password dialog.
        module.MigrationWindow._refresh = lambda self: None
        window = module.MigrationWindow(sudo=False)
        window.show_all()
        window.collected = [json.dumps([{"kind": "stick", "path": "/dev/loop0p4", "model": "",
                                         "size": "4G", "encrypted": False, "version": "0.0.9"}])]
        window._on_candidates(0)
        check(len(window.sources.get_children()) == 1, "the window lists the candidate it was given")
        window.candidate = window.sources.get_row_at_index(0).candidate
        window.collected = [json.dumps(inventory)]
        window._on_inventory(0)
        check(window.stack.get_visible_child_name() == "choose", "reading an inventory moves to the choose page")
        chosen = window._chosen_ids()
        check("home.files.Documents" in chosen and "home.settings..cache" not in chosen,
              "the tree starts from the inventory's defaults")
        # Untick the home files heading and every child follows.
        parent = window.store.get_iter_first()
        while window.store[parent][0] != "home.files":
            parent = window.store.iter_next(parent)
        window._on_toggled(None, window.store.get_path(parent))
        check("home.files.Documents" not in window._chosen_ids(), "a heading toggles its children")
        plan = module.plan_document(window.candidate, window._chosen_ids(), window.inventory_data)
        check(plan["source"] == "/dev/loop0p4" and plan["firstboot"] is False, "the plan names the source")
        window.destroy()
    finally:
        server.terminate()
        server.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--encrypt", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or sys.platform != "linux":
        raise SystemExit("needs Linux and root; run inside the container harness")

    run(["umount", str(MOUNT)])
    run(["cryptsetup", "close", MAPPING])
    run(["userdel", "-r", USER])
    clear_local_accounts()

    install_portlin_packages()
    loop, root = make_old_stick(args.encrypt)
    try:
        inventory = check_stick_to_stick(root, args.encrypt)
        check_archive_round_trip()
        if not args.encrypt:
            check_the_window(inventory)
    finally:
        run(["umount", str(MOUNT)])
        run(["cryptsetup", "close", "portlin-migrate-source"])
        run(["cryptsetup", "close", MAPPING])
        run(["losetup", "-d", loop])
        DISK.unlink(missing_ok=True)
        run(["userdel", "-r", USER])

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: migrated from a real stick and from an archive, source untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
