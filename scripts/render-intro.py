#!/usr/bin/env python3
"""Render portlin-intro's frames to PNG files, with no X server.

The film is a pure function of time (Film.draw), so any frame can be painted
onto an image surface and looked at. Needs python3-gi, python3-gi-cairo and
the fonts the stick ships, so it runs in a Debian container rather than on a
Mac:

  docker run --rm -v "$PWD:/src" -w /src debian:trixie bash -c '
    apt-get update -qq && apt-get install -y -qq python3-gi python3-gi-cairo \
      gir1.2-pango-1.0 fonts-dejavu fonts-liberation2 &&
    python3 scripts/render-intro.py -o out/intro --at 3 8 20'

--fps renders the whole film instead, for ffmpeg to stitch into a video:

  python3 scripts/render-intro.py -o out/intro --fps 30
  ffmpeg -framerate 30 -i out/intro/frame-%05d.png -pix_fmt yuv420p intro.mp4
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime" / "portlin-intro"


def load_tool():
    # The film imports devices and hostinfo from /usr/lib/portlin on a stick;
    # here they come from the runtime directory beside it.
    sys.path.insert(0, str(TOOL.parent))
    loader = importlib.machinery.SourceFileLoader("portlin_intro", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # Registered before running, because dataclasses looks its module up.
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


# What a stick and a machine might say, so the frames show the measured
# drive and the machine's rows without the container being either.
SAMPLE_FACTS = [
    ("machine", "LENOVO ThinkPad X1 Carbon Gen 9"),
    ("cpu", "11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz (8 threads)"),
    ("memory", "15.4G"),
    ("firmware", "UEFI 64-bit"),
]


def sample_drive(intro, kind: str):
    if kind == "none":
        return None
    gib, mib = 1024**3, 1024**2
    disk = 64_023_222_272  # what a "64 GB" stick really holds
    root = disk - 1538 * mib if kind == "expanded" else 8 * gib - 1538 * mib
    encrypted = kind != "plain"
    parts = (
        intro.Partition("sdb1", "EF02", 1 * mib, "", False),
        intro.Partition("sdb2", "vfat", 512 * mib, "/boot/efi", False),
        intro.Partition("sdb3", "ext4", 1 * gib, "/boot", False),
        intro.Partition("sdb4", "LUKS2" if encrypted else "ext4", root, "/", True),
    )
    unclaimed = 0 if kind == "expanded" else disk - 8 * gib
    return intro.Drive("sdb", disk, parts, unclaimed, encrypted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--size", default="1920x1080")
    parser.add_argument("--lang", default="en", help="the account's language, e.g. de")
    parser.add_argument("--label", default="alex@portlin")
    parser.add_argument("--at", type=float, nargs="*", default=[], help="seconds")
    parser.add_argument("--fps", type=int, default=0, help="render every frame")
    parser.add_argument(
        "--drive", choices=["encrypted", "plain", "expanded", "none"], default="encrypted",
        help="the sample stick to measure: an unexpanded encrypted 64 GB one by default",
    )
    parser.add_argument("--no-facts", action="store_true", help="leave the machine out")
    args = parser.parse_args()

    intro = load_tool()
    import cairo

    width, height = (int(n) for n in args.size.split("x"))
    flyby, final = intro.order_words(args.lang)
    film = intro.Film(flyby, final, args.label,
                      drive=sample_drive(intro, args.drive),
                      facts=None if args.no_facts else SAMPLE_FACTS)
    args.out.mkdir(parents=True, exist_ok=True)

    if args.fps:
        frames = [i / args.fps for i in range(int(film.duration * args.fps) + 1)]
        names = [f"frame-{i:05d}.png" for i in range(len(frames))]
    else:
        frames = args.at
        names = [f"t{t:05.2f}.png" for t in frames]

    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    for t, name in zip(frames, names):
        cr = cairo.Context(surface)
        opacity = 1 - intro.ease_in_out(
            intro.progress(t, film.duration - intro.FADE_OUT, intro.FADE_OUT))
        film.draw(cr, width, height, t, opacity=opacity)
        surface.write_to_png(str(args.out / name))
    print(f"{len(frames)} frame(s), film is {film.duration:.1f}s, "
          f"{len(film.words)} words fly past", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
