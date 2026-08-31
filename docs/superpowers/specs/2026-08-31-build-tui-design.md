# A build TUI with real progress

## The problem

`portlin build` is a 20-40 minute operation on native Linux and well over an hour
emulated on an arm64 Mac. For all of that time it prints nothing useful, because
`Runner.run` captures subprocess output rather than streaming it. The operator
cannot distinguish a working build from a hung one, and has no idea how long is
left.

Two failed attempts at watching a build from outside the process motivated this:
polling `du` on the chroot from a second terminal, and grepping a log. Both are
sidecars guessing at another process's internals. The signal already exists
inside the build; it is being thrown away.

## What this adds

A single entry point, `scripts/build.py`, that runs the whole pipeline
(build, write, verify) and renders a terminal UI: a stage per row with a real
progress bar, elapsed time and ETA, the current activity, and a tail of the
underlying command output.

Every percentage is a measurement, not a phase guess. That is the point of the
design, and it is what forces a change to `Runner`.

## Where the numbers come from

| Stage | Source | Denominator |
|---|---|---|
| debootstrap | `I: Retrieving/Validating/Extracting <pkg>` on stderr | package count observed during retrieval |
| packages | `apt-get -o APT::Status-Fd=1` emitting `pmstatus:<pkg>:<pct>:<desc>` | apt's own, covers download, unpack and configure |
| tarball | `tar --checkpoint --checkpoint-action=echo` | `du -sb` of the work dir, taken by the caller |
| unpack | same | uncompressed size recorded at pack time |
| total | per-stage weights | timings cache from previous runs |

`APT::Status-Fd=1` is the load-bearing trick: pointing apt's machine-readable
status stream at stdout means it arrives on a pipe that is already being read.
No `pass_fds`, no extra plumbing.

The overall ETA is the only estimate rather than a measurement. Default weights
ship with the code; each successful run writes real per-stage durations to a
cache and subsequent runs use those. Estimated figures are prefixed with a
tilde in the UI so the distinction is visible rather than implied.

## Components

### `portlin/progress.py` (new)

Pure functions and a state machine. No I/O, no terminal, no subprocess. Turns a
line of command output into a typed event, tracks stage state, and formats bars,
durations and ETAs.

This is where the testable substance lives, following the treatment of
`templates.py` and `layout.py`: the interesting logic is a pure function of its
input, so it is asserted on directly rather than through a subprocess.

### `portlin/runner.py` (changed)

Gains an optional `on_output` callback. When set, `run` uses `Popen` with
`selectors` over both pipes instead of `subprocess.run`, dispatching each line
to the callback as it arrives while still accumulating stdout and stderr
separately for `CommandResult`.

Separately, not merged: `CommandError` quotes stderr, and merging the streams
would change the text of every failure message in the tool. `selectors` rather
than threads because the stdlib supports it directly and the failure modes of
two reader threads plus a timeout are not worth the trouble.

The change is additive. With no callback the code path is unchanged.

### `portlin/chroot.py` (changed)

`apt()` passes `-q -o APT::Status-Fd=1`. `-q` suppresses apt's own progress
rendering, which is meaningless without a terminal and would otherwise be noise
in the tail pane.

Passed unconditionally rather than only when a progress hook is attached, so
that the recorded command list stays a function of the build configuration
alone. A command that varies with runtime state is harder to reason about from
a dry run, which is the one place the whole pipeline is inspectable at once.

### `portlin/rootfs.py`, `portlin/install.py` (changed)

The two `tar` invocations gain `--checkpoint=2000 --checkpoint-action=echo`.
2000 records is 1 MiB of tar stream, frequent enough to animate and rare enough
not to flood the pipe.

### `scripts/build.py` (new)

The entry point. Three responsibilities, in order:

1. **Host detection.** x86_64 Linux running as root builds directly. Anything
   else re-execs this same script inside a privileged `linux/amd64`
   `debian:trixie` container with the repo bind-mounted, and renders the same UI
   from in there.
2. **Wiring.** Builds `BuildConfig` and `WriteConfig`, constructs a `Runner`
   with the progress callback attached, and calls `build_rootfs` and
   `write_stick` directly. It imports portlin rather than parsing its log
   output.
3. **Rendering.** Redraws on change, at most ten times a second.

### `Makefile`

A `make image` target.

## Behaviour outside a terminal

Piped or redirected output degrades to plain timestamped lines, one per stage
transition plus periodic progress. `NO_COLOR` and `TERM=dumb` are honoured. A
build log full of escape sequences and repainted frames is worse than no UI at
all.

## Deliberately excluded

`--encrypt`. It prompts for a passphrase on the terminal, which fights a redraw
loop for the same lines. An unencrypted stick is offered LUKS on first boot with
a passphrase the operator types themselves, which is the better path anyway.

## Testing

`tests/test_progress.py` covers the parsers and formatters as pure functions:
apt status lines including malformed ones, debootstrap lines, tar checkpoints,
the stage state machine, ETA arithmetic, and bar rendering at the boundaries.

The existing suites gain: a `Runner` streaming case asserting the callback sees
lines in order and that `CommandResult` still separates stdout from stderr, and
`rootfs`/`install` cases asserting the checkpoint and status-fd options are
present. The dry-run trace tests already cover the command list and will catch
an accidental change to what gets run.

`scripts/build.py` itself is deliberately thin: detection, wiring, and a redraw
loop, all of which need a terminal or a container to mean anything.
