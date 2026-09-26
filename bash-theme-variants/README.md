# Bash theme concepts

Mockups for the portlin terminal theme: the welcome banner shown when a
terminal opens, and the interactive prompt. Rendered by `render-variants.py`
(design previews, not screenshots — no Debian needed):

```sh
uv run --with pillow python bash-theme-variants/render-variants.py
```

All three use the shipped 16-color palette from
`portlin/resources/runtime/theme/terminalrc` (37 muted, 97 paper, 34 blue,
36 cyan, 32 green), so they look right in xfce4-terminal and degrade cleanly
on the Linux console. The accent (31) appears only for root: the last bar of
the mark, the root prompt glyph, and the "LUKS2 encrypted" state.

## Variant 1 — partition prompt

`variant-1-partition-prompt.png`

Two-line prompt built from the mark: `╭─ user@host ─ path ─ (branch)` over
`╰─ ▂▄▆█ $`. The four bars carry the partition-map rank order; the last one
turns accent and `$` turns `#` for root. Alternate style — the shipped
default is variant 4.

## Variant 2 — hairline

`variant-2-hairline.png`

Single-line prompt with muted gaps; banner is three hairline rows, a disk
meter and a closing rule. Quietest option; same data. Alternate style.

## Variant 3 — fastfetch

`variant-3-fastfetch.png`

The same data through upstream `fastfetch` (added to `packages.py` TOOLS)
with portlin's mark as a custom ASCII logo — this is "portlin in fastfetch".
Ships as `~/.config/fastfetch/config.jsonc` + `portlin.txt` in
`/etc/skel/.config/`, so running `fastfetch` out of the box is themed; the
bashrc banner itself stays `portlin-welcome`: zero dependencies, brand-exact,
and it cannot break when fastfetch changes output. Also the source of the
shipped default's classic `user@host:path$` prompt.

## Variant 4 — combined (shipped default)

`variant-4-combined.png`

Variant 1's welcome banner with variant 3's classic prompt: cyan user, paper
host, blue path, and the accent reserved for root's `#`. Implemented by
`templates.render_bashrc()` and `portlin/resources/runtime/portlin-welcome`;
switch by editing the `PS1=` block in `templates.render_bashrc()`.
