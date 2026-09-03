# Portlin Software: an installer for common apps and drivers

## Context

Portlin writes a real Debian trixie + Xfce install onto a USB stick. Installing
anything beyond the shipped set today means a terminal, `sudo`, and knowing
which vendor repository, key and package name each app wants. This adds a
"Software" entry to the applications menu that installs common software in one
click, and a "This machine" scan that reads the hardware the stick is currently
booted on and offers the drivers that fit it, the GPU driver above all.

Decisions already taken with the user:

- Catalog: the named apps (Mullvad VPN, qBittorrent, Deluge, Tor Browser,
  Chrome, Chromium, Brave, Pale Moon, Zed, Cursor, Claude Desktop, Claude Code,
  Kimi Code, RustDesk, AnyDesk) plus dev tools (VS Code, Docker, Tailscale,
  Syncthing, Wireshark), media and office (VLC, LibreOffice, GIMP, OBS Studio,
  Thunderbird, KeePassXC) and chat (Signal, Telegram, Discord). No Steam, no
  games category, so no i386 multiarch handling.
- New images enable Debian's `non-free` component by default; the installer
  also adds it on demand on sticks written before this change.
- The window carries Install and Remove per entry and one "Update everything"
  button that runs `apt-get full-upgrade`.

**Tier:** everything here is updatable tier. A broken installer is a program
that refuses to run, never a stick that will not boot, so it ships in
`portlin-runtime` (root CLI + catalog) and `portlin-desktop` (GTK window),
exactly like `portlin-encrypt` and `portlin-about` today. Nothing frozen
(`fstab`, `crypttab`, GRUB, initramfs, the wizard) is touched.

**Architecture:** two programs on the existing runtime/desktop split.
`portlin-install` (portlin-runtime) is the only thing that runs as root; it
owns every privileged action and speaks a small `::` line protocol on stdout.
`portlin-software` (portlin-desktop) is a GTK3 front end that only ever runs
`portlin-install`, via `pkexec` (or `sudo -n` when first boot waived the sudo
password) for privileged entries and directly for per-user ones. Both read one
pure catalog module at `/usr/lib/portlin/catalog.py`, beside `devices.py`.

Repo conventions that apply (see `docs/superpowers/plans/2026-08-31-portlin-runtime-packages.md`):
stdlib only; runtime tools use `subprocess` directly like `portlin-encrypt`;
unit tests run on macOS with gi stubbed (`tests/test_caffeine.py::_stub_gi`),
tools loaded via `SourceFileLoader` (`tests/test_runtime_tools.py::_load_tool`);
no en/em dashes; conventional commits, no AI trailers; GPL header on every
shipped script; comments explain the code. `portlin-desktop` Depends may only
name packages `packages.resolve()` installs (offline chroot install, guarded by
`tests/test_package.py::test_every_dependency_is_already_in_the_rootfs`);
`portlin-runtime` Depends must be satisfiable from `MINIMAL_GROUPS`
(`test_packages_installed_on_a_minimal_stick_need_no_desktop`). Also copy this
plan to `docs/superpowers/plans/2026-09-02-portlin-software.md` in the first
commit, as the house convention.

## Design decisions

1. **Privilege boundary.** `portlin-install` refuses privileged kinds when not
   root (exit 3) and per-user kinds when root (exit 3), so `sudo portlin-install
   install zed` cannot scatter root-owned files in a home. The invoking user
   comes from `PKEXEC_UID`, then `SUDO_UID`; group additions (docker,
   wireshark) act on that user.
2. **Elevation.** `pkexec /usr/bin/portlin-install ...` with a polkit action
   (`org.portlin.install`, `auth_admin_keep`, `exec.path` annotation) so a few
   installs ask once. The GUI probes `sudo -n -v` at startup and uses
   `["sudo", "-n"]` instead when it succeeds (the wizard's NOPASSWD waiver).
3. **pkexec and the agent.** `pkexec` is its own package in trixie and is not
   in `packages.py`; `mate-polkit` (the prompt-drawing agent) only arrives as a
   Recommends of `xfce4`. Both go into `packages.DESKTOP` explicitly;
   `portlin-desktop` Depends on `pkexec` and Recommends `mate-polkit`.
4. **Components.** Entries needing a component the stick lacks get a deb822
   drop-in `/etc/apt/sources.list.d/portlin-components.sources` carrying the
   union of missing components per (URI, suite) parsed from
   `/etc/apt/sources.list` (fallback: `VERSION_CODENAME` + deb.debian.org).
   No `Signed-By` needed: Debian archive keys are already trusted.
   `config.DEFAULT_COMPONENTS` gains `non-free` for new images (own commit).
5. **Vendor keys** are stored as served, no gnupg: armoured keys as `*.asc`,
   binary ones as `*.gpg`, and the sources line's `signed-by=` names that path.
6. **No pipe-to-shell.** Vendor installer scripts (Zed, Kimi Code) are fetched
   with `curl -fsSL --retry 3 -L -o FILE`, size-checked, then run with
   `sh FILE`. The catalog stores URLs, never shell strings.
7. **Exact removal.** Every install writes a JSON record (packages installed,
   files written, dirs created) to `/var/lib/portlin/software/<id>.json` (root)
   or `$XDG_STATE_HOME/portlin/software/<id>.json` (user); `remove` undoes the
   record, falling back to catalog defaults.
8. **Cursor and Pale Moon.** Cursor has no verified direct .deb URL: it ships
   as kind `web` (GUI opens the download page) with a discovery step to promote
   it to `deb-url`. Pale Moon has no repo any more: kind `tarball-opt` from the
   official mirror redirect, validated with `tar -tJf` before extraction, with
   a generated `.desktop` file.
9. **Update everything** is `portlin-install upgrade`, which runs
   `apt-get update` then `apt-get full-upgrade` through the same executor and
   protocol.

## Catalog

`check` is the installed test: `dpkg:<names>` = `dpkg-query` status
`installed` for any name; `path:<p>` = path exists (`~` expanded for the
invoking user). Categories in display order: Browsers, Communication, Media,
Office and graphics, Security and privacy, Networking, Development, AI tools,
Remote access, System tools, Drivers.

| id | name | kind | category | source details | check |
|---|---|---|---|---|---|
| chromium | Chromium | apt | Browsers | chromium | dpkg:chromium |
| chrome | Google Chrome | deb-url | Browsers | https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb (adds its own repo) | dpkg:google-chrome-stable |
| brave | Brave | apt-repo | Browsers | key https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg -> /usr/share/keyrings/brave-browser-archive-keyring.gpg; sources_url https://brave-browser-apt-release.s3.brave.com/brave-browser.sources -> /etc/apt/sources.list.d/brave-browser-release.sources; pkg brave-browser | dpkg:brave-browser |
| tor-browser | Tor Browser | apt | Browsers | torbrowser-launcher; needs_components (contrib) | dpkg:torbrowser-launcher |
| palemoon | Pale Moon | tarball-opt | Browsers | https://www.palemoon.org/download.php?mirror=us&bits=64&type=linuxgtk3 -> /opt/palemoon (strip 1); generated /usr/share/applications/portlin-palemoon.desktop, Exec=/opt/palemoon/palemoon %u, icon under /opt/palemoon (confirm in discovery) | path:/opt/palemoon/palemoon |
| signal | Signal | apt-repo | Communication | key https://updates.signal.org/desktop/apt/keys.asc -> /usr/share/keyrings/signal-desktop-keyring.asc; `deb [arch=amd64 signed-by=/usr/share/keyrings/signal-desktop-keyring.asc] https://updates.signal.org/desktop/apt xenial main` -> /etc/apt/sources.list.d/signal-xenial.list; pkg signal-desktop | dpkg:signal-desktop |
| discord | Discord | deb-url | Communication | https://discord.com/api/download?platform=linux&format=deb (redirects) | dpkg:discord |
| telegram | Telegram | apt | Communication | telegram-desktop | dpkg:telegram-desktop |
| thunderbird | Thunderbird | apt | Communication | thunderbird | dpkg:thunderbird |
| vlc | VLC | apt | Media | vlc | dpkg:vlc |
| obs-studio | OBS Studio | apt | Media | obs-studio | dpkg:obs-studio |
| audacity | Audacity | apt | Media | audacity | dpkg:audacity |
| libreoffice | LibreOffice | apt | Office and graphics | libreoffice, libreoffice-gtk3 | dpkg:libreoffice |
| gimp | GIMP | apt | Office and graphics | gimp | dpkg:gimp |
| inkscape | Inkscape | apt | Office and graphics | inkscape | dpkg:inkscape |
| keepassxc | KeePassXC | apt | Security and privacy | keepassxc | dpkg:keepassxc |
| mullvad | Mullvad VPN | apt-repo | Security and privacy | key https://repository.mullvad.net/deb/mullvad-keyring.asc -> /usr/share/keyrings/mullvad-keyring.asc; `deb [signed-by=/usr/share/keyrings/mullvad-keyring.asc arch=amd64] https://repository.mullvad.net/deb/stable stable main` -> /etc/apt/sources.list.d/mullvad.list; pkg mullvad-vpn | dpkg:mullvad-vpn |
| tailscale | Tailscale | apt-repo | Security and privacy | key https://pkgs.tailscale.com/stable/debian/{codename}.noarmor.gpg -> /usr/share/keyrings/tailscale-archive-keyring.gpg; sources_url https://pkgs.tailscale.com/stable/debian/{codename}.tailscale-keyring.list -> /etc/apt/sources.list.d/tailscale.list; pkg tailscale | dpkg:tailscale |
| qbittorrent | qBittorrent | apt | Networking | qbittorrent | dpkg:qbittorrent |
| deluge | Deluge | apt | Networking | deluge | dpkg:deluge |
| syncthing | Syncthing | apt | Networking | syncthing; notes: start with `systemctl --user enable --now syncthing` | dpkg:syncthing |
| filezilla | FileZilla | apt | Networking | filezilla | dpkg:filezilla |
| wireshark | Wireshark | apt | Networking | wireshark; debconf `wireshark-common wireshark-common/install-setuid boolean true`; add_groups (wireshark) | dpkg:wireshark |
| vscode | Visual Studio Code | apt-repo | Development | key https://packages.microsoft.com/keys/microsoft.asc -> /usr/share/keyrings/microsoft.asc; `deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.asc] https://packages.microsoft.com/repos/code stable main` -> /etc/apt/sources.list.d/vscode.list; pkg code | dpkg:code |
| zed | Zed | user-script | Development | https://zed.dev/install.sh; remove_paths ~/.local/zed.app, ~/.local/bin/zed, ~/.local/share/applications/zed.desktop | path:~/.local/bin/zed |
| cursor | Cursor | web | Development | https://cursor.com/download (discovery may promote to deb-url) | dpkg:cursor |
| docker | Docker | apt | Development | docker.io; add_groups (docker); notes: log out and in for the group | dpkg:docker.io |
| build-tools | Build tools | apt | Development | build-essential, pkg-config, git | dpkg:build-essential |
| claude-desktop | Claude Desktop | apt-repo | AI tools | key https://downloads.claude.ai/claude-desktop/key.asc -> /usr/share/keyrings/claude-desktop-archive-keyring.asc; `deb [signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] https://downloads.claude.ai/claude-desktop/apt/stable stable main` -> /etc/apt/sources.list.d/claude-desktop.list; pkg claude-desktop | dpkg:claude-desktop |
| claude-code | Claude Code | apt-repo | AI tools | key https://downloads.claude.ai/keys/claude-code.asc -> /etc/apt/keyrings/claude-code.asc (fingerprint 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE in notes); `deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main` -> /etc/apt/sources.list.d/claude-code.list; pkg claude-code | dpkg:claude-code |
| kimi-code | Kimi Code | user-script | AI tools | https://code.kimi.com/kimi-code/install.sh; check and remove_paths set by discovery | path:~/.local/bin/kimi (confirm) |
| rustdesk | RustDesk | github-deb | Remote access | repo rustdesk/rustdesk, asset `^rustdesk-\d+\.\d+\.\d+-x86_64\.deb$` via https://api.github.com/repos/rustdesk/rustdesk/releases/latest | dpkg:rustdesk |
| anydesk | AnyDesk | apt-repo | Remote access | key https://keys.anydesk.com/repos/DEB-GPG-KEY -> /etc/apt/keyrings/keys.anydesk.com.asc; `deb [signed-by=/etc/apt/keyrings/keys.anydesk.com.asc] https://deb.anydesk.com all main` -> /etc/apt/sources.list.d/anydesk-stable.list; pkg anydesk | dpkg:anydesk |
| remmina | Remmina | apt | Remote access | remmina, remmina-plugin-rdp, remmina-plugin-vnc | dpkg:remmina |
| flatpak | Flatpak and Flathub | apt | System tools | flatpak; post_install `flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo` | dpkg:flatpak |
| gparted | GParted | apt | System tools | gparted | dpkg:gparted |
| tmux | tmux | apt | System tools | tmux (also the harness's small end-to-end package) | dpkg:tmux |
| nvidia-driver | NVIDIA proprietary driver | apt | Drivers | resolver nvidia-detect: install nvidia-detect, run it, install the metapackage it names plus linux-headers-amd64 and linux-headers-$(uname -r); needs_components (non-free); warning below | dpkg:nvidia-driver, nvidia-tesla-470-driver |
| intel-graphics | Intel graphics acceleration | apt | Drivers | intel-media-va-driver, i965-va-driver, mesa-vulkan-drivers, vulkan-tools, libva-utils | dpkg:intel-media-va-driver |
| amd-graphics | AMD graphics acceleration | apt | Drivers | mesa-va-drivers, mesa-vulkan-drivers, vulkan-tools, libva-utils, firmware-amd-graphics | dpkg:mesa-vulkan-drivers |
| broadcom-wifi | Broadcom STA wifi driver | apt | Drivers | broadcom-sta-dkms, linux-headers-amd64; needs_components (contrib); warning below | dpkg:broadcom-sta-dkms |
| printing | Printing and scanning | apt | Drivers | cups, system-config-printer, printer-driver-all, simple-scan, sane-airscan | dpkg:cups |

Warning texts (shown in a dialog before install, verbatim in the catalog):

- nvidia-driver: "This installs NVIDIA's proprietary driver for the card in
  this machine and turns off the open nouveau driver. On machines with no
  NVIDIA card the open drivers keep working. On a machine with an NVIDIA card
  this driver does not support, the desktop may not start. If that happens,
  press Ctrl+Alt+F2, log in, and run: sudo portlin-install remove
  nvidia-driver. The driver is built as a kernel module on this stick, is not
  signed, and will not load on a machine with Secure Boot turned on."
- broadcom-wifi: "Only for Broadcom chips the built-in drivers do not handle.
  Built as a kernel module on this stick, unsigned, so it will not load with
  Secure Boot on."
- zed, kimi-code: "Runs the vendor's installer script as you, under your home
  directory. Portlin downloads it first and runs the file it downloaded."

---

### Task 1: Prerequisite packages and the non-free decision

**Files:** modify `portlin/packages.py`, `portlin/config.py`; tests
`tests/test_packages.py`, `tests/test_config.py` (also grep
`tests/test_templates.py`, `tests/test_rootfs.py` for the literal component
string).

**Interfaces:** `packages.SYSTEM` gains `pciutils` (moved out of `TOOLS`, so a
`--minimal` stick has `lspci` and portlin-runtime may depend on it);
`packages.DESKTOP` gains `pkexec` and `mate-polkit` (comment: pkexec is a
separate package in trixie; the agent is a Recommends of xfce4 today and a line
in someone else's package is not a promise);
`config.DEFAULT_COMPONENTS = "main contrib non-free non-free-firmware"` with
the comment updated (non-free carries the NVIDIA driver; enabling it installs
nothing by itself).

- [ ] Tests: `test_a_minimal_stick_can_identify_its_hardware` (pciutils in
  `resolve(MINIMAL_GROUPS)`); `TestSoftwareApp::test_the_desktop_can_ask_for_a_password`
  (pkexec, mate-polkit, polkitd in `resolve()`); `test_elevation_is_desktop_only`
  (neither in minimal); `test_new_images_can_reach_non_free`.
- [ ] Implement; `make test`.
- [ ] Two commits: `feat(packages): carry pkexec, mate-polkit and pciutils for the Software app`
  and `feat(config): enable non-free in new images`.

### Task 2: The shared catalog module

**Files:** create `portlin/resources/runtime/catalog.py`, `tests/test_catalog.py`.

**Interfaces** (pure, stdlib only, no portlin import; GPL header as a comment):

```python
KINDS = ("apt", "apt-repo", "deb-url", "github-deb", "tarball-opt", "user-script", "web")
PRIVILEGED_KINDS = frozenset({"apt", "apt-repo", "deb-url", "github-deb", "tarball-opt"})
USER_KINDS = frozenset({"user-script"})
CATEGORIES: tuple[str, ...]   # display order above

@dataclass(frozen=True)
class Repo:
    key_url: str; keyring_path: str; sources_path: str
    sources_line: str | None = None    # exactly one of these two
    sources_url: str | None = None     # "{codename}" substituted at install time

@dataclass(frozen=True)
class Check:
    kind: str                          # "dpkg" | "path"
    values: tuple[str, ...]            # any one satisfied means installed

@dataclass(frozen=True)
class Entry:
    id: str; name: str; summary: str; category: str; kind: str; check: Check; homepage: str
    packages: tuple[str, ...] = (); repo: Repo | None = None; url: str | None = None
    github_repo: str | None = None; asset_pattern: str | None = None
    opt_dir: str | None = None; launcher: str | None = None; icon: str | None = None
    needs_components: tuple[str, ...] = (); resolver: str | None = None   # "nvidia-detect"
    debconf: tuple[str, ...] = (); add_groups: tuple[str, ...] = ()
    post_install: tuple[tuple[str, ...], ...] = (); remove_paths: tuple[str, ...] = ()
    warning: str | None = None; notes: str | None = None

ENTRIES: tuple[Entry, ...]
def by_id(entry_id) -> Entry                       # KeyError on unknown
def by_category() -> dict[str, list[Entry]]        # CATEGORIES order
def search(query, entries=ENTRIES) -> list[Entry]  # name, summary, id, packages
def is_privileged(entry) -> bool
def parse_dpkg_status(text) -> set[str]            # from dpkg-query -W -f='${Package} ${db:Status-Status}\n'
def installed(entry, dpkg_installed: set[str], home: Path) -> bool
def validate(entries=ENTRIES) -> list[str]         # every problem as text; [] when clean
def to_dict(entry) -> dict                         # for --json
```

`validate` rules (one test each, built with `dataclasses.replace` on a good
entry): unique lowercase ids; category in CATEGORIES; kind in KINDS; every URL
(including inside `sources_line`) is https; package names match
`^[a-z0-9][a-z0-9+.-]+$`; per-kind required fields (`apt-repo` has exactly one
of sources_line/sources_url, keyring under `/usr/share/keyrings/` or
`/etc/apt/keyrings/`, sources under `/etc/apt/sources.list.d/`, and
`signed-by=<keyring_path>` inside sources_line when present; `tarball-opt` has
opt_dir under `/opt/` and a launcher; `user-script` has a `path` check under
`~`; `web` has url only); every entry has a check; resolver is None or
`"nvidia-detect"`; no U+2013/U+2014 anywhere.

- [ ] Tests: `test_it_compiles`, `test_validate_is_clean`, the rule tests,
  `test_search_matches_name_summary_and_package`,
  `test_by_category_follows_display_order`,
  `test_parse_dpkg_status_keeps_only_installed`, `test_installed_dpkg_any_of`,
  `test_installed_path_expands_home`, `test_every_named_entry_is_present`
  (literal id list from the table), `test_privileged_and_user_kinds_partition_the_kinds`
  (web in neither).
- [ ] Write the module with ENTRIES from the table.
- [ ] Discovery in `docker run --rm -it --platform linux/amd64 debian:trixie`
  (`apt-get install -y curl ca-certificates tar xz-utils`): Cursor direct .deb
  URL (promote to deb-url if stable, record the package name from
  `dpkg-deb -f`); Pale Moon tarball top-level dir, binary and icon paths;
  Kimi Code and Zed install locations as a non-root user (set `check` and
  `remove_paths`); copy the PCI ID table from
  `/usr/share/doc/broadcom-sta-dkms/README.Debian` into `BROADCOM_WL_IDS`
  (Task 4).
- [ ] `make test`; commit `feat(runtime): add the software catalog as a shared module`.

### Task 3: `portlin-install`, the root-capable installer

**Files:** create `portlin/resources/runtime/portlin-install`,
`tests/test_install_tool.py`; add `"portlin-install"` to `TOOLS` in
`tests/test_runtime_tools.py` (factor `_load_tool` into `tests/conftest.py`).

**CLI:**

```
portlin-install list [--json] [--category NAME]
portlin-install show <id> [--json]
portlin-install status <id>... [--json]       exit 0 if all installed, else 1
portlin-install install [--dry-run] <id>...
portlin-install remove [--dry-run] <id>
portlin-install upgrade [--dry-run]           apt-get update; apt-get full-upgrade
portlin-install scan [--json] [--from FILE]   Task 4
```

Exit codes: 0 ok; 1 a step failed; 2 usage/unknown id; 3 privilege mismatch;
4 kind `web` (prints the URL).

**stdout protocol** (every `::` line flushed; everything else is raw tool
output passed through):

```
::step <text>          ::progress <0-100>        ::warn <text>
::reboot               ::result ok|failed <id> [text]
```

**Pure functions** (module level, unit-tested):

```python
@dataclass(frozen=True)
class Step:
    text: str; argv: tuple[str, ...] | None = None
    write: tuple[str, str] | None = None; mkdir: str | None = None
    remove: tuple[str, ...] = (); check_file: str | None = None
    env: tuple[tuple[str, str], ...] = ()

@dataclass(frozen=True)
class Context:
    root: bool; codename: str; components: tuple[str, ...]
    user: str | None; home: str; kernel: str; download_dir: str

APT_BASE = ("apt-get", "-y", "-q", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", "-o", "DPkg::Lock::Timeout=300")
APT_ENV = (("DEBIAN_FRONTEND", "noninteractive"),)

def apt_argv(*args) -> tuple[str, ...]
def curl_argv(url, dest) -> tuple[str, ...]            # curl -fsSL --retry 3 -L -o dest url
def invoking_user(environ) -> str | None               # PKEXEC_UID, then SUDO_UID, via pwd
def parse_sources_list(text) -> list[tuple[str, str, tuple[str, ...]]]
def missing_components(needed, enabled) -> tuple[str, ...]
def render_component_sources(entries, components) -> str   # deb822, one stanza per (uri, suite)
def plan_components(entry, ctx) -> list[Step]
def plan_install(entry, ctx, *, resolved_packages=None) -> list[Step]
def plan_remove(entry, ctx, record: dict | None) -> list[Step]
def plan_upgrade(ctx) -> list[Step]
def pick_release_asset(payload: dict, pattern: str) -> str  # ValueError if none or many
def render_desktop_entry(entry) -> str                      # tarball-opt launcher
def headers_package(kernel) -> str                          # linux-headers-<uname -r>
def parse_nvidia_detect(text) -> str | None                 # Task 4
def needs_reboot(steps) -> bool                             # any *-dkms or nvidia-detect argv
def format_event(kind, *parts) -> str                       # the only place "::" is spelled
```

`plan_install` per kind, in order: `plan_components` (write drop-in, then
`apt-get update`, or nothing); `apt-repo`: mkdir keyring parent, curl key,
`check_file`, write `sources_line` (codename substituted) or curl
`sources_url` + `check_file`, `apt-get update`, `apt-get install <packages>`;
`apt`: `debconf-set-selections` from a written temp file when `debconf` set;
if resolver is nvidia-detect: `apt-get install nvidia-detect`, run it, feed
output to `parse_nvidia_detect`, re-plan with `resolved_packages` (`--dry-run`
prints `<metapackage from nvidia-detect>`); for any `*-dkms` package or
`linux-headers-amd64` add `headers_package(ctx.kernel)` and `::warn` when
`apt-cache policy` has no candidate ("reboot into the newest kernel, then
install again"); `apt-get install <packages>`; `post_install` argvs;
`usermod -aG <group> <user>` per group, or `::warn` when user unknown;
`deb-url`: curl to `<download_dir>/<id>.deb`, `check_file`, `apt-get install
<path>`, remove file; `github-deb`: fetch release JSON with `urllib.request`
(User-Agent `portlin-install`), `pick_release_asset`, then as deb-url;
`tarball-opt`: curl, `tar -tJf` validate, mkdir opt_dir, `tar -xJf -C opt_dir
--strip-components=1`, write `/usr/share/applications/portlin-<id>.desktop`,
remove download; `user-script`: mkdir `$XDG_CACHE_HOME/portlin`, curl,
`check_file`, `sh <path>`, remove; `web`: exit 4.

`plan_remove`: apt-ish kinds purge the recorded packages (else catalog
packages), `apt-get autoremove`, then remove `sources_path` and
`keyring_path` for apt-repo; `tarball-opt` removes opt_dir and the desktop
file; `user-script` removes `remove_paths` or warns that removal is manual.

Executor `run_plan(steps, *, dry_run, log)`: `Popen` with stdout+stderr
merged and streamed; for `apt-get` steps pass `-o APT::Status-Fd=<w>` over an
`os.pipe()` (with `pass_fds`) and turn `pmstatus:<pkg>:<pct>:<desc>` into
`::progress`; "Waiting for cache lock" lines become a `::step waiting for
another package manager`. Non-zero exit stops the plan with `::result failed`.
Success writes the state record and prints `::reboot` (if `needs_reboot`) then
`::result ok`. Downloads: `/var/cache/portlin/downloads` (root) or
`$XDG_CACHE_HOME/portlin` (user).

Component drop-in shape for a portlin sources.list adding non-free:

```
# Written by portlin-install. Adds archive components the stick was built
# without. Delete this file to take them away again.
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: non-free

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: non-free
```

- [ ] Tests (fixture ctx: root, trixie, `("main","contrib","non-free-firmware")`,
  user "ben", kernel "6.12.30-amd64"): apt_argv waits for the lock and never
  prompts; curl follows redirects, fails on HTTP errors, retries;
  invoking_user prefers pkexec then sudo then None; parse_sources_list reads
  `templates.render_sources_list()` output; missing_components empty when all
  enabled; component drop-in exact text; plan_components empty for a
  main-only entry and writes the drop-in then updates otherwise; apt kind
  installs exactly its packages with no update; apt-repo order key -> sources
  -> update -> install with `check_file` on the keyring and signed-by equal to
  keyring_path; tailscale URLs substitute the codename; brave/tailscale curl
  the vendor sources file; chrome installs the downloaded file through
  apt-get not dpkg and removes it; pick_release_asset picks the x86_64 deb
  among aarch64/rpm siblings and raises on none; palemoon validates before
  extracting and strips the top directory; render_desktop_entry parses and
  points into /opt; user-script downloads then runs the file, no `|` or
  `bash -c` in any argv; wireshark preseeds debconf before installing and adds
  the group; docker adds the invoking user to docker and warns with no user;
  nvidia installs the detector first and the headers for the running kernel
  (with `resolved_packages=("nvidia-driver",)`); dkms entries set
  needs_reboot; remove purges what the record says and drops repo files;
  remove of a user-script with no paths only warns; privileged entries refuse
  without root and user entries refuse as root (SystemExit 3, Popen
  monkeypatched to raise); web entries print the URL and exit 4; dry-run
  prints every argv and runs nothing; every `"::` literal in the source is
  inside format_event; `list --json` carries installed state; plan_upgrade is
  update then full-upgrade.
- [ ] Write the tool (GPL header, docstring on the privilege boundary and
  protocol, `sys.path.insert(0, "/usr/lib/portlin")`, `from catalog import`).
  I/O only in `load_context()`, `run_plan()`, `dpkg_installed()`, the GitHub
  fetch, state records, `main()`.
- [ ] `make test`; commit `feat(runtime): add portlin-install, the root-capable software installer`.

### Task 4: The hardware scan

**Files:** modify `portlin-install`, `tests/test_install_tool.py`.

```python
@dataclass(frozen=True)
class PciDevice: slot: str; class_code: str; class_name: str; vendor: str; device: str; name: str
GPU_CLASSES = {"0300", "0302", "0380"}; WIFI_CLASS = "0280"
VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel", "14e4": "broadcom"}
BROADCOM_WL_IDS = {...}   # provisional; replaced from broadcom-sta-dkms README.Debian in Task 2 discovery
LSPCI_LINE = re.compile(r"^(?P<slot>\S+) (?P<class_name>.+?) \[(?P<class>[0-9a-f]{4})\]: (?P<name>.+?) \[(?P<vendor>[0-9a-f]{4}):(?P<device>[0-9a-f]{4})\]")
def parse_lspci(text) -> list[PciDevice]
def parse_nvidia_detect(text) -> str | None    # token between "It is recommended to install the" and "package"
def recommend(devices, *, nvidia_detect_output) -> dict
```

`scan --json` document: `{"gpus": [{slot, vendor, name, id}], "wifi": [...],
"suggestions": [{"entry", "reason", "detail"?}], "notes": [...]}`. Rules:
10de GPU -> nvidia-driver (detail names the nvidia-detect metapackage when
available, else "install to find the exact driver"); 8086 GPU ->
intel-graphics; 1002 GPU -> amd-graphics; 14e4 wifi with device in
BROADCOM_WL_IDS -> broadcom-wifi; two GPUs -> hybrid laptop note; no GPU ->
note "no display controller reported by lspci". `scan` runs `lspci -nn` (or
`--from FILE`); runs `nvidia-detect` if present and an NVIDIA GPU was found;
missing `lspci` yields empty lists and a note, exit 0.

- [ ] Tests with two captured lspci texts (Intel+NVIDIA laptop with BCM43224
  `14e4:4353`; QEMU `1234:1111`): parse reads slot/class/vendor/device;
  ignores lines without ids; nvidia suggests the driver; intel+nvidia notes a
  hybrid; qemu suggests nothing; broadcom wl ids suggest sta and others do
  not; nvidia-detect output parsing (nvidia-driver, nvidia-tesla-470-driver,
  unsupported -> None); scan json is stable with four keys; `scan --from`
  needs no lspci.
- [ ] Implement; `make test`; commit `feat(runtime): scan the machine for drivers in portlin-install`.

### Task 5: Ship the installer, the catalog and the polkit action in portlin-runtime

**Files:** create `portlin/resources/runtime/org.portlin.install.policy`,
`tests/test_software_policy.py`; modify `portlin/package.py`,
`tests/test_package.py`.

`package.py`: `TOOLS` gains `portlin-install`; new `SHARED_MODULES =
["devices.py", "catalog.py"]` replacing the hardcoded devices.py line (loop to
`usr/lib/portlin/<name>`); new `POLKIT_ACTIONS = {"org.portlin.install.policy":
"usr/share/polkit-1/actions/org.portlin.install.policy"}` (in runtime, beside
the program it names, so the exec.path can never disagree); runtime Depends
gain `pciutils`, `curl`.

Policy: `<action id="org.portlin.install">`, vendor Portlin, description
"Install or remove software on this stick", `icon_name` portlin, defaults
`auth_admin` / `auth_admin` / `auth_admin_keep`, annotation
`org.freedesktop.policykit.exec.path` = `/usr/bin/portlin-install`. XML
comment must not contain `--`.

- [ ] Tests: rename the three-tools test to four; catalog shipped beside
  devices.py and not executable; polkit action shipped; runtime depends on
  lspci and curl. `test_software_policy.py` parses the XML: action id,
  auth_admin_keep, exec.path equals a path in `text_files("portlin-runtime")`
  and `executable_paths("portlin-runtime")`, icon_name == `package.APP_ICON`.
- [ ] Implement; `make test`; `make dryrun` shows the new files in the write plan.
- [ ] Commit `feat(package): ship portlin-install, the catalog and its polkit action`.

### Task 6: `portlin-software`, the GTK front end

**Files:** create `portlin/resources/runtime/portlin-software`, `tests/test_software.py`.

```python
APP_ID = "org.portlin.Software"; INSTALLER = "/usr/bin/portlin-install"; ICON_NAME = "portlin"
def elevation_argv(passwordless_sudo) -> list[str]        # ["sudo","-n"] or ["pkexec"]
def launch_argv(entry_or_action, action, *, passwordless_sudo) -> list[str]  # web raises ValueError
def parse_event(line) -> tuple[str, str] | None            # None for raw output
def explain_exit(code, kind) -> str                        # 126/127 under pkexec: cancelled / no agent; 3; 4
def row_state(entry, installed, busy) -> tuple[str, str, bool]   # status, button label, sensitive
def visible_entries(query, category, entries) -> list[Entry]
def suggestion_rows(scan: dict, entries_by_id) -> list[tuple[Entry, str]]   # unknown ids skipped
```

Window (`SoftwareWindow(Gtk.ApplicationWindow)`, `Gtk.Application` with
APP_ID so a second launch raises the first, as caffeine does): header bar with
`Gtk.SearchEntry` and an "Update everything" button; left `Gtk.ListBox` of
categories; right `Gtk.ListBox` of rows (bold name, summary, status label,
one button: Install / Remove / Get for web); a details bar showing notes,
homepage and what it installs; the Drivers page opens with a "This machine"
box fed by `portlin-install scan --json` run through `Popen` +
`GLib.io_add_watch` at startup ("Looking at this machine..." meanwhile),
suggested entries first with reasons; bottom `Gtk.Revealer` with the `::step`
status line, a `Gtk.ProgressBar` (pulsing until the first `::progress`) and a
monospace log `Gtk.TextView`. One job at a time (`row_state` makes other
buttons insensitive). Entries with `warning` show a `Gtk.MessageDialog`
first. On `::result` the installed state is re-queried (`dpkg-query -W` once
plus path checks); `::reboot` shows an info bar. Startup: `set_default_icon_name(ICON_NAME)`;
probe `sudo -n -v` (3 s timeout, failure means False). Colours: stay with the
GTK theme; no accent red (reserved for the encrypted root per docs/brand).

- [ ] Tests (gi stubbed as in `test_caffeine.py`): compiles, python3 shebang,
  imports, no `geteuid` in source; launch_argv for tmux is
  `["pkexec", INSTALLER, "install", "tmux"]`, with passwordless sudo
  `["sudo","-n",...]`, for zed no elevation, for cursor raises; INSTALLER
  equals the policy's exec.path parsed from the resource file; parse_event on
  each `::` form and None on an apt line; row_state cases; search "torrent"
  finds qbittorrent and deluge; a category with empty query lists only that
  category; suggestion_rows skips unknown ids; explain_exit 126/127 mention
  authentication, 3 mentions root.
- [ ] Write the program (GPL header; docstring: never runs apt, never needs root).
- [ ] `make test`; commit `feat(desktop): add the Software app`.

### Task 7: Ship the app in portlin-desktop with a menu entry

**Files:** create `portlin/resources/runtime/portlin-software.desktop`;
modify `portlin/package.py`, `tests/test_package.py`, `tests/test_software.py`.

Desktop entry: Name=Software, Comment="Install common programs, and the
drivers this machine needs", Exec=portlin-software, Icon=portlin,
Terminal=false, StartupNotify=true, `Categories=System;PackageManager;`
(System rather than Settings, with the same reasoning as caffeine's comment),
Keywords covering software, install, apps, drivers, nvidia, vpn, browser.

`package.py`: `DESKTOP_TOOLS` gains `portlin-software`; `MENU_ENTRIES` gains
the entry; desktop Depends gain `pkexec`, control gets
`recommends=["mate-polkit"]` (Recommends so a person running another agent is
not forced to remove portlin-desktop).

- [ ] Tests: desktop ships the app and its entry; depends on pkexec;
  recommends an agent; runtime never carries it. `TestMenuEntry` in
  `test_software.py` mirroring `test_about.py`: valid entry, Exec names a
  shipped binary, Icon is a hicolor name with no slash, Terminal=false,
  System in Categories.
- [ ] Implement; `make test`; commit `feat(package): put Software in the applications menu`.

### Task 8: Prove it in the container and on an image

**Files:** create `scripts/test-software.py`; modify `Makefile` (harness
target), `scripts/verify-image.sh`, `tests/test_harness_scripts.py`.

Harness (root, debian:trixie, network; runs before `test-package-upgrade.py`,
which purges portlin packages first): build the packages with `python3 -m
portlin package`, install them, assert the four new files exist; write a
plain portlin-style `sources.list` in the container, use
`render_component_sources` for real and `apt-get update`; for every `apt`
entry's Debian package `apt-cache policy` must show a Candidate (collect all
failures, report together); end to end `install tmux` (exit 0, `::step`
present, ends `::result ok tmux`), `status` 0, `remove`, `status` 1; end to
end `install tailscale` (keyring and list exist after, gone after remove;
print `skip:` if pkgs.tailscale.com is unreachable); privilege: `su nobody
-c "portlin-install install tmux"` exits 3, `install zed` as root exits 3;
`scan --json` parses with empty suggestions and a note; GUI under Xvfb as in
`test-caffeine.py`: construct `SoftwareWindow` with a fake scan dict, assert
row count equals `len(catalog.ENTRIES)`, search "torrent" leaves two rows,
destroy.

verify-image.sh: extend the tool loop with `portlin-install`; assert
`usr/lib/portlin/catalog.py`, the policy file and `usr/bin/lspci` exist; in
the desktop block assert `portlin-software` executable, its .desktop present,
`usr/bin/pkexec` executable, and
`etc/xdg/autostart/polkit-mate-authentication-agent-1.desktop` present (and
if it has `OnlyShowIn`, that it includes XFCE).

- [ ] Write the harness; add `pciutils xz-utils` to the Makefile harness
  apt line and `python3 -u scripts/test-software.py && \` before the upgrade
  harness; update the count comment (seven harnesses, ten runs).
- [ ] `tests/test_harness_scripts.py::TestSoftwareHarness`: parses, no
  hardcoded versions, tmux and tailscale are catalog ids, remove follows install.
- [ ] `make check`; `make harness`.
- [ ] Commit `test: prove the Software app and portlin-install against a real apt`.

### Task 9: Documentation

- [ ] README: a "Software" section after the caffeine paragraph (what it
  installs and from where; the Drivers page and the NVIDIA caveat; that all
  privilege goes through `portlin-install`, usable from a terminal; the
  non-free drop-in for old sticks). Update "Updates" to name
  `portlin-install` and Software; add `test-software.py` to the harness table;
  "Seven harnesses, ten runs".
- [ ] docs/design.md: add "the software catalog and the Software app" to the
  Updatable row, plus a short "Privilege lives in one place" paragraph under
  the tier rule.
- [ ] Commit `docs: describe the Software app`.

## Verification

1. `make test` passes on macOS (new: test_catalog, test_install_tool,
   test_software, test_software_policy).
2. `make check` (shellcheck on verify-image.sh).
3. `make harness`: ten runs including test-software.py.
4. `make image` (a cached tarball from before Task 1 lacks pkexec and
   mate-polkit, so the offline install of portlin-desktop would fail against
   it), then `sudo scripts/verify-image.sh out/stick.img`.
5. `scripts/qemu-boot-test.sh out/stick.img` still reaches GRUB on both
   firmware paths (regression check; nothing frozen changed).
6. Manual in QEMU (`-vga virtio`, user networking) after first boot: open
   Software; Drivers page reports no suggestion for the virtio GPU; search
   tmux, Install, the mate-polkit prompt appears, the log streams apt, the
   row flips to Installed; Remove; Zed installs with no prompt into
   `~/.local/bin/zed`; NVIDIA entry shows its warning dialog; "Update
   everything" streams full-upgrade; `portlin-install scan` in a terminal
   matches the page. Then boot the stick on a real NVIDIA machine and confirm
   the scan names the card and the nvidia-detect metapackage.

## Risks

- **apt lock contention**: `DPkg::Lock::Timeout=300` waits rather than
  fails; the step line says so.
- **DKMS and headers**: `linux-headers-amd64` tracks the newest installed
  kernel, not the running one after an un-rebooted upgrade; the installer
  also asks for `linux-headers-$(uname -r)` and warns if absent; every DKMS
  install ends with `::reboot`.
- **Secure Boot**: unsigned modules will not load; out of scope, stated in
  the warning text.
- **NVIDIA portability**: the driver blacklists nouveau everywhere the stick
  boots; the warning carries the text-console recovery command and `remove`
  purges exactly what was installed.
- **Vendor scripts**: downloaded to a file and size-checked, but still remote
  code run on request; the warning says so.
- **Vendor drift**: URLs and package names are vendor promises; the harness
  resolves every Debian name each run and exercises one Debian and one vendor
  entry end to end. Cursor stays `web` until a stable URL is found.
- **Offline sticks**: apt's own failure text is passed through; the scan
  still works.
- **Cached rootfs tarballs** predating Task 1 cannot satisfy the new Depends
  in write's offline chroot; rebuild with `make image`.

---

## What changed while this was built

The catalog above was written from documentation. Building it meant checking
each entry against a real `debian:trixie` container, and six things were not
what the documentation said.

- **Telegram has no Debian package.** `telegram-desktop` is not in trixie, so
  the entry became `tarball-opt` against Telegram's own build, which unpacks
  to `/opt/telegram` and updates itself.
- **`libva-utils` is a source package.** The binary is `vainfo`, in both the
  Intel and AMD entries.
- **`nvidia-tesla-470-driver` is not in trixie.** The legacy metapackage
  there is `nvidia-tesla-535-driver`, which is what the entry's check names.
- **Cursor does publish a stable download.** The URL its own updater uses
  serves the current `.deb`, so the entry is `deb-url` rather than a page to
  open in a browser. That left the `web` kind with no entries, and it was
  removed rather than kept as an unreachable branch.
- **Kimi Code installs to `~/.kimi-code`,** not `~/.local/bin`, and adds that
  directory to `PATH` in `~/.profile`. Removal cannot undo the `PATH` line,
  which the entry's notes now say. Its installer also needs `bash`, which is
  what every vendor script is now run with.
- **The wl driver's PCI table claims every Broadcom device,** so it cannot
  say which chips actually need it. The suggestion list is curated and says
  plainly that it is only needed if wifi does not work.

Two facts the plan assumed were confirmed rather than changed: `mate-polkit`
does ship the autostart entry that draws pkexec's prompt (in
`mate-polkit-common`, which it depends on), and every vendor repository in
the table resolves, accepts its own signature, and carries the package name
the catalog claims.

One bug outside this work was found and fixed on the way: `test-encrypt-hook.py`
leaves a breadcrumb in `/run/portlin`, and `test-stash-passphrase.py` then
tried to remove that directory as if it owned it, so `make harness` failed on
the harness before it rather than on anything it was testing.
