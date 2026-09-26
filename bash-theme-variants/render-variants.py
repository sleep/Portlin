#!/usr/bin/env python3
"""Render the portlin bash-theme concept mockups.

These are design previews, not screenshots: each PNG draws a terminal window
with one welcome-banner + prompt concept at the brand palette. Regenerate with

    uv run --with pillow python bash-theme-variants/render-variants.py

The brand colors duplicated here are the tokens from docs/brand/README.md and
the 16-color palette in portlin/resources/runtime/theme/terminalrc.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"

# Brand tokens (docs/brand/README.md).
INK = "#12161C"
PANEL = "#1A212B"
LINE = "#2C3542"
MUTED = "#7C8B9E"
PAPER = "#E8EDF3"
ACCENT = "#FF3355"
# The rest of the shipped terminal palette (terminalrc).
GREEN = "#4FBF8B"
BLUE = "#5AA9E6"
CYAN = "#57C7C7"
BRIGHT_GREEN = "#74D9AB"

SCALE = 2  # draw at 2x, downsample for retina-crisp text

FONT_SIZE = 13
TITLE_SIZE = 12
LINE_H = 21
PAD = 16
TITLE_H = 30

R = PAPER, True
N = PAPER, False


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size * SCALE, index=1 if bold else 0)


def mark_art() -> list[list[tuple[str, str]]]:
    """The mark as terminal runs: rounded box, four bottom-anchored bars.

    Bar widths 2:5:7:12 carry the partition-map rank order of the real mark
    (1 MiB EF02 / 512 MiB ESP / 1 GiB boot / root) into character cells, and
    the bars sit on the bottom edge of the enclosure like the drawn mark.
    """
    bars = [(2, MUTED), (5, MUTED), (7, MUTED), (12, ACCENT)]
    row_runs: list[tuple[str, str]] = []
    inner_width = 0
    for i, (width, color) in enumerate(bars):
        row_runs.append(("█" * width, color))
        inner_width += width
        if i < len(bars) - 1:
            row_runs.append(("   ", INK))
            inner_width += 3

    def bar_row() -> list[tuple[str, str]]:
        return [("│", MUTED), (" ", INK), *row_runs,
                (" " * (inner_width - sum(len(r[0]) for r in row_runs)), INK),
                (" ", INK), ("│", MUTED)]

    empty = [("│", MUTED), (" " * (inner_width + 2), INK), ("│", MUTED)]
    return [
        [("╭" + "─" * (inner_width + 2) + "╮", MUTED)],
        empty,
        bar_row(),
        bar_row(),
        bar_row(),
        [("╰" + "─" * (inner_width + 2) + "╯", MUTED)],
    ]


def info_column(rows: list[list]) -> list[list]:
    """Pad the mark art and lay an info column to its right, vertically centred."""
    art = mark_art()
    art_width = max(sum(len(run[0]) for run in row) for row in art)
    offset = max(0, (len(rows) - len(art)) // 2)
    combined = []
    for i in range(max(len(rows), offset + len(art))):
        left = art[i - offset] if offset <= i < offset + len(art) else []
        right = rows[i] if i < len(rows) else []
        pad = " " * (art_width + 6 - sum(len(run[0]) for run in left))
        combined.append([*left, (pad, INK), *right])
    return combined


def kv(key: str, value: list[tuple[str, str, bool]]) -> list:
    return [(f"{key:<8} ", MUTED), *value]


def banner_rows_v1() -> list[list]:
    rows = [
        [("portlin ", PAPER, True), ("0.1.2", MUTED, False)],
        kv("os", [("Debian GNU/Linux 13 (trixie)", PAPER, False)]),
        kv("machine", [("ThinkPad X270", PAPER, False), ("   UEFI, 64-bit", MUTED, False)]),
        kv("cpu", [("Intel Core i5-7300U (4 threads)", PAPER, False)]),
        kv("memory", [("3.2G ", PAPER, False), ("of ", MUTED, False), ("15.5G", PAPER, False)]),
        kv("disk", [("41.2G ", GREEN, True), ("free of ", MUTED, False), ("58.0G", PAPER, False)]),
        kv("root", [("LUKS2 encrypted", ACCENT, False)]),
        kv("uptime", [("2 hours, 14 mins", PAPER, False)]),
        kv("shell", [("bash 5.2.37", PAPER, False)]),
    ]
    return info_column(rows)


def footer() -> list[list]:
    return [
        None,
        [("run ", MUTED, False), ("portlin-info", PAPER, False),
         (" for the full report", MUTED, False)],
    ]


def prompt_v1(user: str, host: str, path: str, branch: str | None = None,
              root: bool = False) -> list[list]:
    bar_color = ACCENT if root else PAPER
    glyph = "#" if root else "$"
    top = [("╭─ ", MUTED, False), (user, PAPER, True), ("@", MUTED, False),
           (host, CYAN, True), (" ─ ", MUTED, False), (path, BLUE, True)]
    if branch:
        top += [(" ─ ", MUTED, False), (f"({branch})", GREEN, False)]
    bottom = [("╰─ ▂▄▆", MUTED, False), ("█ ", bar_color, False),
              (f"{glyph} ", PAPER, True)]
    return [top, bottom]


def session_v1() -> list[list]:
    lines = banner_rows_v1() + footer() + [None]
    lines += prompt_v1("alice", "portlin", "~")
    lines[-1] += [("ls projects", PAPER, False)]
    lines += ls_output() + [None]
    lines += prompt_v1("alice", "portlin", "~/projects", "main")
    lines[-1] += [("sudo -s", PAPER, False)]
    lines += [None]
    lines += prompt_v1("root", "portlin", "/home/alice/projects", "main", root=True)
    return lines


def ls_output() -> list[list]:
    def row(mode: str, name: str, color: str, target: str = "") -> list:
        runs = [(mode + "  ", MUTED, False), ("3 alice alice 4096 Sep 26 ", MUTED, False)]
        if target:
            runs += [(name + " ", MUTED, False), ("-> " + target, CYAN, False)]
        else:
            runs += [(name, color, False)]
        return runs

    return [
        [("total 12", MUTED, False)],
        row("drwxr-xr-x", "designs/", BLUE),
        row("drwxr-xr-x", "dotfiles/", BLUE),
        row("-rw-r--r--", "notes.md", PAPER),
        [("lrwxrwxrwx", MUTED, False), ("  1 alice alice   15 Sep 24 ", MUTED, False),
         ("stick", CYAN, False), (" -> ", MUTED, False), ("/media/portlin/", CYAN, False)],
    ]


def banner_rows_v2() -> list[list]:
    sep = ("   ", MUTED, False)
    rows = [
        [("portlin ", PAPER, True), ("0.1.2", MUTED, False), sep,
         ("Debian GNU/Linux 13 (trixie)", MUTED, False), (" · kernel 6.12.35-amd64", MUTED, False),
         (" · up 2 hours, 14 mins", MUTED, False)],
        [("machine ", MUTED, False), ("ThinkPad X270", PAPER, False),
         (" (UEFI, 64-bit)", MUTED, False), SEP := sep,
         ("cpu ", MUTED, False), ("Intel Core i5-7300U (4 threads)", PAPER, False), sep,
         ("memory ", MUTED, False), ("3.2G of 15.5G", PAPER, False)],
        [("disk ", MUTED, False), ("/  ", PAPER, False), ("41.2G ", BRIGHT_GREEN, True),
         ("free of 58.0G", MUTED, False), ("  ", MUTED, False),
         ("████████", GREEN, False), ("────", LINE, False),
         ("   root ", MUTED, False), ("LUKS2 encrypted", ACCENT, False)],
        [("─" * 88, LINE, False)],
    ]
    return rows


def prompt_v2(user: str, host: str, path: str, branch: str | None = None,
              root: bool = False) -> list[list]:
    glyph = ("#" if root else "$", ACCENT if root else PAPER, True)
    line = [(user, PAPER, True), ("@", MUTED, False), (host, CYAN, True),
            ("  ", MUTED, False), (path, BLUE, False)]
    if branch:
        line += [("  ", MUTED, False), (f"({branch})", GREEN, False)]
    line += [("  ", MUTED, False), glyph, (" ", PAPER, False)]
    return [line]


def session_v2() -> list[list]:
    lines = banner_rows_v2() + [None]
    lines += prompt_v2("alice", "portlin", "~")
    lines[-1] += [("ls projects", PAPER, False)]
    lines += ls_output() + [None]
    lines += prompt_v2("alice", "portlin", "~/projects", "main")
    lines[-1] += [("sudo -s", PAPER, False)]
    lines += prompt_v2("root", "portlin", "/home/alice/projects", "main", root=True)
    return lines


def banner_rows_v3() -> list[list]:
    rows = [
        [("alice", PAPER, True), ("@", MUTED, False), ("portlin", CYAN, True)],
        [("─" * 14, LINE, False)],
        kv("os", [("Debian GNU/Linux 13 (trixie)", PAPER, False)]),
        kv("host", [("ThinkPad X270 (UEFI, 64-bit)", PAPER, False)]),
        kv("kernel", [("6.12.35-amd64", PAPER, False)]),
        kv("uptime", [("2 hours, 14 mins", PAPER, False)]),
        kv("packages", [("612 (dpkg)", PAPER, False)]),
        kv("shell", [("bash 5.2.37", PAPER, False)]),
        kv("wm", [("Xfwm4", PAPER, False)]),
        kv("cpu", [("Intel Core i5-7300U (4)", PAPER, False)]),
        kv("memory", [("3.2 GiB / 15.5 GiB", PAPER, False)]),
        kv("disk (/)", [("41.2 GiB free of 58.0 GiB", BRIGHT_GREEN, True)]),
        kv("portlin", [("0.1.2", PAPER, False), ("  root: ", MUTED, False),
                       ("LUKS2 encrypted", ACCENT, False)]),
    ]
    return info_column(rows)


def prompt_v3(user: str, host: str, path: str, root: bool = False) -> list[list]:
    glyph = ("#" if root else "$", ACCENT if root else PAPER, True)
    return [[(user, CYAN, True), ("@", MUTED, False), (host, PAPER, True),
             (":" + path, BLUE, False), glyph, (" ", PAPER, False)]]


def session_v3() -> list[list]:
    lines = banner_rows_v3() + footer() + [None]
    lines += prompt_v3("alice", "portlin", "~")
    lines[-1] += [("ls projects", PAPER, False)]
    lines += ls_output() + [None]
    lines += prompt_v3("alice", "portlin", "~/projects")
    lines[-1] += [("sudo -s", PAPER, False)]
    lines += prompt_v3("root", "portlin", "/home/alice/projects", root=True)
    return lines


def session_combined() -> list[list]:
    """The shipped default: variant 1's banner, variant 3's classic prompt."""
    lines = banner_rows_v1() + footer() + [None]
    lines += prompt_v3("alice", "portlin", "~")
    lines[-1] += [("ls projects", PAPER, False)]
    lines += ls_output() + [None]
    lines += prompt_v3("alice", "portlin", "~/projects")
    lines[-1] += [("sudo -s", PAPER, False)]
    lines += prompt_v3("root", "portlin", "/home/alice/projects", root=True)
    return lines


def draw_window(lines: list[list], title: str) -> Image.Image:
    body = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    cell_w = body.textlength("M", font=font(FONT_SIZE))
    cols = max(
        (sum(len(run[0]) for run in row) for row in lines if row), default=10
    )
    rows = len(lines)
    width = int(PAD * 2 + cols * cell_w + 8 * SCALE)
    height = (TITLE_H + PAD + rows * LINE_H + PAD - 6) * SCALE

    page_w, page_h = 1680, 1050
    page = Image.new("RGB", (page_w * SCALE, page_h * SCALE), "#0D1015")
    pd = ImageDraw.Draw(page)
    for x in range(0, page_w, 48):
        pd.line([(x * SCALE, 0), (x * SCALE, page_h * SCALE)], fill="#141A22")
    for y in range(0, page_h, 48):
        pd.line([(0, y * SCALE), (page_w * SCALE, y * SCALE)], fill="#141A22")

    wx = (page_w * SCALE - width) // 2
    wy = max(60 * SCALE, (page_h * SCALE - height) // 2)
    shadow = Image.new("RGBA", page.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [wx, wy + 10 * SCALE, wx + width, wy + 10 * SCALE + height],
        12 * SCALE, fill=(0, 0, 0, 110))
    page.paste(Image.alpha_composite(page.convert("RGBA"), shadow.filter(
        ImageFilter.GaussianBlur(16 * SCALE))).convert("RGB"), (0, 0))
    pd = ImageDraw.Draw(page)

    pd.rounded_rectangle([wx, wy, wx + width, wy + height], 12 * SCALE,
                         fill=INK, outline=LINE, width=SCALE)
    pd.rounded_rectangle([wx, wy, wx + width, wy + TITLE_H * SCALE], 12 * SCALE,
                         fill=PANEL)
    pd.rectangle([wx, wy + TITLE_H * SCALE - 8 * SCALE, wx + width,
                  wy + TITLE_H * SCALE], fill=PANEL)
    pd.line([(wx, wy + TITLE_H * SCALE), (wx + width, wy + TITLE_H * SCALE)],
            fill=LINE, width=SCALE)

    bx = wx + 14 * SCALE
    base = wy + (TITLE_H - 8) * SCALE
    for i, bw in enumerate((1, 3, 5, 8)):
        pd.rectangle([bx, base - 11 * SCALE, bx + bw * SCALE, base],
                     fill=ACCENT if i == 3 else MUTED)
        bx += (bw + 2) * SCALE
    pd.text((bx + 8 * SCALE, wy + 8 * SCALE), title, font=font(TITLE_SIZE),
            fill=MUTED)

    y = wy + TITLE_H * SCALE + PAD * SCALE - 2 * SCALE
    x0 = wx + PAD * SCALE
    for row in lines:
        if row is not None:
            x = x0
            for run in row:
                text, color = run[0], run[1]
                bold = len(run) > 2 and run[2]
                pd.text((x, y), text, font=font(FONT_SIZE, bold), fill=color)
                x += body.textlength(text, font=font(FONT_SIZE, bold))
            end_x = x
        y += LINE_H * SCALE
    if lines and lines[-1]:
        pd.rectangle([end_x + 2 * SCALE, y - (LINE_H - 4) * SCALE,
                      end_x + 5 * SCALE, y - 5 * SCALE], fill=PAPER)
    return page.resize((page_w, page_h), Image.LANCZOS)


def caption(name: str, blurb: str, tag: str) -> None:
    pass  # captions live in the README; the PNGs stay pure mocks


VARIANTS = {
    "variant-1-partition-prompt.png": (
        session_v1, "Terminal — alice@portlin: ~"),
    "variant-2-hairline.png": (
        session_v2, "Terminal — alice@portlin: ~"),
    "variant-3-fastfetch.png": (
        session_v3, "Terminal — alice@portlin: ~"),
    "variant-4-combined.png": (
        session_combined, "Terminal — alice@portlin: ~"),
}


def main() -> None:
    for filename, (session, title) in VARIANTS.items():
        image = draw_window(session(), title)
        image.save(HERE / filename, optimize=True)
        print("wrote", filename)


if __name__ == "__main__":
    main()
