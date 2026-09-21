# Migrating from one portlin to another

A portlin stick is a real install, so the things that make it yours accumulate
on it: an account, a home directory, saved wifi passwords, whatever Software
installed. A new stick starts with none of that. This design adds a way to plug
the old stick into a machine booted from the new one and bring across whatever
you choose, and a way to save the same things to an archive and restore them
from it later.

The governing rule is that the source is never written to. The old stick, or the
archive, is opened read-only, read, and closed; the only drive that changes is
the one that is running.

## Tier

Everything here is updatable, in the sense of the tier rule in
`2026-08-31-portlin-runtime-updates-design.md`: it ships in `portlin-runtime` and
`portlin-desktop`. A broken migration tool cannot stop a stick booting.

The one frozen file touched is the first-boot wizard, which gains a screen that
offers a migration. The wizard runs the packaged tool as a subprocess and reads
JSON from it. It does not import the tool's module, because the wizard is
whatever `write` froze and the package is whatever apt has brought forward, and
a subprocess with a fixed argument set is the boundary that survives the two
drifting. If the tool is missing or prints something the wizard does not
understand, the wizard skips the screen and carries on as it does today.

## Components

| Program | Package | Runs as | Purpose |
|---|---|---|---|
| `portlin-migrate` | `portlin-runtime` | root | the only thing that opens devices and writes files; fixed verbs |
| `/usr/lib/portlin/migrate.py` | `portlin-runtime` | imported | inventory, plan, the rsync and tar command lines, their output parsers, appliers; pure over paths and a runner |
| `portlin-migration` | `portlin-desktop` | the user | the *Migrate* window; reads `portlin-migrate`'s output |
| `org.portlin.migrate.policy` | `portlin-runtime` | polkit | lets the window run `portlin-migrate` as root, `exec.path` pinned |
| `portlin-firstboot` | frozen | root | one extra screen that runs `portlin-migrate` |

### `portlin-migrate` verbs

```
portlin-migrate candidates [--json]          # portlin drives and archives in reach
portlin-migrate inventory SOURCE [--json]    # what SOURCE holds, by item, with sizes
portlin-migrate apply SOURCE --plan FILE     # copy what the plan selects
portlin-migrate export FILE                  # this stick, everything, to an archive
portlin-migrate restore [SOURCE]             # the whiptail flow, start to finish
```

`SOURCE` is a block device (`/dev/sdb4`), an archive path, or the word `auto`
for the single candidate when there is exactly one. Every verb that opens a LUKS
source reads the passphrase from stdin, never from an argument, for the reason
the README gives for `write` having no `--passphrase` flag.

`apply` streams the line protocol `portlin-install` established, so the window
reads it with the same code Software uses:

```
::step <text>            a phase began
::progress <0-100>       bytes copied over bytes planned
::warn <text>            something was skipped and the run went on
::result ok|failed [text]
```

Exit codes are `portlin-install`'s: 0 ok, 1 failed, 2 usage, 3 privilege, and a
new 6, *no space*, for a plan that does not fit.

### The plan is the seam

The wizard needs the selection early, to prefill its screens, but the copy late,
after the account exists so files land under the right uid. The window makes the
selection as the user but copies as root. The CLI wants both in one go. A plan
file is what crosses each of those boundaries: `inventory` produces the items,
something chooses among them, `apply` takes the chosen ids. The destructive half
is therefore replayable from a file and testable on its own.

## Finding and opening the source

### Candidates

`lsblk --json -o NAME,LABEL,FSTYPE,PKNAME,SIZE,MODEL,TRAN`, over every disk that
is not the one `/` is on. `devices.root_source()` already knows which that is;
the exclusion is by disk rather than partition, so the running stick's own
`/boot` is never offered.

A disk is a portlin stick when its third partition carries the `portlin-boot`
label, because that label is written by `write` and is readable whether or not
the root is encrypted. The fourth partition is then the root: `FSTYPE=ext4` with
the `portlin-root` label is plaintext, `crypto_LUKS` is encrypted.

Archives are candidates too: any `*.portlin-backup.tar.zst` directly under a
mount in `/media/*/` or `/run/media/*/`, and any path given explicitly.

Each candidate is reported with the disk model and size, the portlin version
from `/etc/portlin-release` once it can be read, and whether it is encrypted.

### Opening

- A plaintext root is mounted `ro,noload` at `/run/portlin/migrate/source`.
  `noload` because a stick pulled out without unmounting has a journal to
  replay, and replaying it is a write.
- An encrypted root is opened with `cryptsetup open --readonly --type luks`
  under the mapping name `portlin-migrate-source`, then mounted the same way.
  A wrong passphrase is a retry; after three the flow returns to the candidate
  list.
- An archive is not unpacked up front. The manifest is its first member and is
  read alone; the chosen members are extracted at apply time.

The source is confirmed as portlin by `/etc/portlin-release`. Its version is
shown beside the running one, and a source newer than the target draws a
warning, because that is the direction in which configuration formats diverge.

A source on the same disk as `/` is refused with a message rather than filtered
out silently, so `portlin-migrate inventory /dev/sda4` on the running stick says
why it will not.

### Teardown

Unmount, close the mapping, remove the mount directory, in that order, in a
`finally`, whether apply finished, failed or was cancelled. The unlocked mapping
never outlives the command that opened it.

## Inventory

The inventory is a JSON list of items:

```json
{"id": "home.files.Documents", "category": "home.files", "label": "Documents",
 "bytes": 1483920, "default": true, "note": ""}
```

The same list feeds the whiptail checklist, the window's tree and the CLI's
`--only`/`--skip`. A plan is the list of chosen ids. Categories, in the order
they are shown:

| Category | Read from the source | Items | Applied |
|---|---|---|---|
| `account` | `/etc/passwd`, `/etc/shadow`, `/etc/group`; `/etc/sudoers.d/50-portlin-nopasswd`; `/etc/lightdm/lightdm.conf.d/10-portlin.conf` | one per account with uid 1000 or above | first boot only, see below |
| `identity` | `/etc/hostname`, `/etc/default/locale`, `/etc/default/keyboard`, `/etc/timezone` | one per setting | first boot: prefilled into the existing screens, applied by the wizard's own appliers. After setup: copies of those appliers in `migrate.py`, the same accepted duplication the tier rule describes for `apply_expand` |
| `home.files` | every non-dot entry directly under the account's home | one per entry | copied |
| `home.settings` | every dot entry directly under the home | one per entry; `.cache` unticked by default; `.portlin-migrate-backup` never listed | copied |
| `software` | `/var/lib/portlin/software/*.json`; `catalog.installed()` against the source's dpkg status and home for entries with no record | one per catalog entry; driver entries unticked, since they describe the old machine | `portlin-install install <id>` per entry, last, output streamed |
| `network` | `/etc/NetworkManager/system-connections/` | one item | copied, `0600 root` |
| `extras` | `/var/lib/bluetooth/`; `/etc/cups/printers.conf` and `/etc/cups/ppd/`; the theme and icon names in the source's `xsettings.xml` | one per line | copied; themes re-applied by name through the wizard's regex targets, only when that theme is installed here |

Sizes come from a walk at inventory time, so the checklist can say
`Downloads  14.2 GB` and the total of the selection can be compared with free
space on `/` before anything is copied. A plan that does not fit is refused
before the first file, with the shortfall named; when the stick has not been
expanded the message says so and names `portlin-expand`, using the same
arithmetic `portlin-info` uses.

### The account

The password moves as its hash, through `chpasswd -e`, so the tool never learns
or shows it. `useradd` is given the source's name, uid, gid, comment and shell;
the group list is the source's intersected with the groups that exist here,
because the set differs between Debian releases and `useradd` fails outright on
an unknown one. The sudo waiver and autologin are applied with the wizard's own
`apply_sudo_password` and `apply_autologin`, which keep their visudo check.

After setup the account exists, so the category is shown greyed with "account
already exists" and home files are chowned to the local uid and gid instead.
When the source home belongs to a different username than the local one, the
files still go into the local home; the label says where.

### Software

Software is restored by re-running the installer rather than by copying `/opt`
or dpkg's database across. What the catalog installs depends on the machine and
on the mirror at the time; the records under `/var/lib/portlin/software` are
the intent, and the intent is the portable part. This needs network. A failing
install is a `::warn` and the run continues; the summary lists what did not
install so it can be retried from Software.

## Copying

Files move by `rsync` from a stick and by `tar` from an archive, and in both
cases every byte is written once, straight to where it will live. `rsync` and
`zstd` join the `system` package group so every stick has them, `--minimal`
included, and `portlin-runtime` depends on both.

### From a stick

One `rsync` per selected item:

```
rsync -a --backup --backup-dir=<backup tree>/<item> --chown=<uid>:<gid>
      --info=progress2 --no-inc-recursive <source item>/ <target item>/
```

- `-a` keeps modes, times, symlinks as symlinks, and never follows a link.
- `--backup --backup-dir` is the move-aside: a file about to be replaced is
  renamed into the backup tree at its own relative path, costing no space and
  no extra pass.
- No `--delete`, so a file that exists only on the target survives; a
  directory on both sides is merged, not replaced.
- `--chown` maps everything under home to the local account in the same pass,
  which is what makes the "account already exists" case one command rather
  than a copy and a walk.
- `--info=progress2 --no-inc-recursive` prints one percentage for the item
  against a total rsync has counted in advance. `migrate.py` reads it and
  weights it by the item's inventory size into the overall `::progress`.

`migrate.py` builds those argument lists and parses that output; it does not
copy files itself. That is the shape `build_rootfs` and `write_stick` have,
and for the same reason: the ordered command list is what the unit tests
assert, and the harness is what proves rsync did what the list says.

### From an archive

No staging copy. `tar -t` lists the chosen members; every one that already
exists on the target is renamed into the backup tree first, mirrored by path;
then `tar -I zstd -x -C / --no-same-owner` extracts the chosen members straight
into place, and each home item gets one `chown -R` to the local account, which
is metadata only. Progress comes from `--checkpoint-action=echo`, read the way the build's
`parse_tar_checkpoint` reads it (a copy of that regex, since `portlin/progress.py`
is host-side and not on the stick), against the byte total the manifest records.

### The backup tree

- under home: `~/.portlin-migrate-backup/<YYYY-MM-DD-HHMMSS>/<item>/<relative path>`
- under `/etc` or `/var`: `/var/backups/portlin-migrate/<timestamp>/<absolute path>`

One timestamp covers the whole run, so one migration is one directory. The
summary names the directory and how many files went there, and says nothing
when nothing did, which on a fresh stick is the usual case. The tool deletes
nothing on the target except by moving it into that tree, and the tree itself
is never listed by `inventory`, so it can never be copied onto itself.

### Idempotence

Running `apply` again with the same plan replaces what it copied last time and
moves the local copies aside again, under a new timestamp. A failed run is
retried by running it again; nothing needs cleaning up first.

## Archives

`export` writes `<hostname>-<YYYY-MM-DD>.portlin-backup.tar.zst` the way
`build` writes the rootfs: `tar -I 'zstd -T0 -3' -cf`, with checkpoints for
progress. Level 3 rather than the rootfs's 6 because a home directory is mostly
already-compressed media and documents, where a higher level costs time and
saves nothing. `manifest.json` is member 0 and holds the inventory, the source
version and hostname, the export time and the uncompressed byte total. The
trees follow under the paths they had on the stick: `home/<user>/...`,
`etc/...`, `var/lib/portlin/software/...`, `var/lib/bluetooth/...`.

Import reads member 0 alone, with `tar -xO`, to build the checklist without
unpacking anything, then extracts as described above.

The manifest carries the account's password hash, because the account is part
of what an archive restores. The file is written `0600`, owned by whoever ran
the export, and the CLI says so on the way out. Exporting to a drive readable
by anyone is the user's call, made with that line in front of them.

## Errors and logging

- An unreadable source file is a `::warn` with the path, taken from rsync's
  own error line; the copy goes on and the count is in the summary. A stick that has been through a bad shutdown
  has a few.
- `ENOSPC` mid-copy stops the run. The summary says what was copied and what
  was not, and the backup tree is left where it is.
- Everything is logged to `/var/log/portlin-migrate.log`, one line per file
  moved aside or skipped, in the form the wizard's log uses.
- Nothing is ever written to the source. The plaintext mount is `ro` and the
  LUKS mapping is `--readonly`, so even a bug cannot.

## The whiptail flow

`portlin-migrate restore`, from a terminal or from the wizard:

1. **Source.** A menu of candidates, one line each:
   `SanDisk Ultra 64G   portlin 0.1.2, encrypted` or
   `backup on MyDrive   office-2026-09-14.portlin-backup.tar.zst`, plus
   *Other path...*. Skipped when `SOURCE` was given on the command line.
2. **Passphrase**, when needed.
3. **What to bring over.** One checklist. Whiptail checklists are flat, so
   categories are non-selectable heading rows rendered as `-- Home: files --`
   and their items sit indented beneath with sizes. Everything is ticked except
   `.cache` and driver entries.
4. **Summary.** Item count, total size, free space after, what will be moved
   aside; confirm.
5. **Progress** through `--gauge` fed from the `::progress` stream, then a final
   screen with the backup directory and any warnings.

In the wizard, steps 1 to 4 run right after Welcome, only when `candidates`
reports something, and step 5 runs in the apply stage immediately after
`apply_account`. The account, hostname, locale, keyboard and time zone screens
are still shown, prefilled from the source, so nothing is applied unseen. The
*Ready to apply* summary gains a line:
`Restore:   12 items, 8.3 GB from SanDisk Ultra 64G`.

## The window

`portlin-migration`, *Migrate* in the System menu, three pages in a stack the
way Software is built:

1. **Source.** The candidate list with a refresh button and *Choose archive...*.
2. **What to bring over.** A `Gtk.TreeView` with a check column and a size
   column; a category row toggles its children. Reading a LUKS stick needs
   root, so `inventory` runs through `pkexec` too, with the passphrase from a
   dialog handed down the child's stdin.
3. **Progress.** The bar, the status line and the log pane Software has, fed
   by `pkexec portlin-migrate apply`.

*Back up this stick...* in the header menu opens a file chooser and runs
`pkexec portlin-migrate export`.

## Packaging

In `portlin/package.py`: `portlin-migrate` joins `TOOLS`, `migrate.py` joins
`SHARED_MODULES`, `org.portlin.migrate.policy` joins `POLKIT_ACTIONS`,
`portlin-migration` joins `DESKTOP_TOOLS` and its desktop file joins
`MENU_ENTRIES`. The wizard change is in `resources/firstboot/portlin-firstboot`.
`rsync` and `zstd` join the `system` group in `portlin/packages.py`, beside
`whiptail` and `pciutils`, and `portlin-runtime`'s `Depends`. The README gains
a *Migrating* section.

## Testing

**Unit** (`tests/test_migrate.py`, no root, runs on macOS):

- inventory over a fake source tree in `tmp_path`, including the hidden-entry
  split, `.cache` unticked, `.portlin-migrate-backup` absent
- plan filtering, `--only` and `--skip`
- the rsync and tar command lists for a plan, recorded through a `Runner`,
  including the backup directory per item, `--chown` only under home, the
  move-aside list computed from a `tar -t` listing, and the order: move
  aside, extract, chown
- the `progress2` and checkpoint parsers, and the weighting into one overall
  percentage
- candidate parsing from canned `lsblk` JSON, including the running-disk
  exclusion and the encrypted-root case
- the free-space refusal and its `portlin-expand` wording
- archive manifest round trip and selective extraction
- `package.py` assertions that every new file ships under the path the
  protocol and the polkit action name
- the wizard's prefill logic with `portlin-migrate` stubbed, and the wizard
  carrying on unchanged when the tool is absent or prints nonsense

**Harness** (`scripts/test-migrate.py`, Docker): a second loop-device stick,
once encrypted and once not, given a canary home, a saved wifi connection and a
software record; `portlin-migrate apply` from a plan; then assert the canary,
the account with the same uid and password hash, the backup tree holding the
replaced file, a target-only file surviving the merge, symlinks still symlinks,
and that the source's mtimes are untouched and its mapping is gone. Then
`export` from the second stick and `apply` from the archive, with the same
assertions, so both copy paths are proven against real files. The window runs against
Xvfb the way `test-software.py` does.

## Out of scope

Migrating between different Debian suites is allowed but not smoothed over;
the version warning is the whole of the help. Migrating a home larger than the
target drive is refused, not partially done. Merging two accounts into one is
not attempted: one source account maps to one local account.
