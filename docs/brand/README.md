# Brand sources

The rendered PNGs are what ship; these are the files they are rendered from. Run
`./render.sh` to rebuild everything into `out/brand/`.

## Tokens

| Token | Value | Use |
|---|---|---|
| ink | `#12161C` | Background |
| panel | `#1A212B` | Background, lit end of the gradient |
| line | `#2C3542` | Grid, hairlines |
| muted | `#7C8B9E` | Unencrypted partitions, labels, mark stroke |
| paper | `#E8EDF3` | Wordmark |
| accent | `#FF3355` | Reserved for the encrypted root partition and `--encrypt` |

Futura for the wordmark, Menlo for every technical label. The accent colour means one
thing only. If it appears on something that is not the LUKS2 root, it is wrong.

## The mark

Four bars anchored at the device start, widths in the true rank order of the partition
table in the top-level README (1 MiB `EF02`, 512 MiB ESP, 1 GiB `/boot`, then root), with
only the last bar in accent because only root is encrypted. It reads at once as a USB-A
connector, a partition map and the expansion ramp. Those widths are meaning, not
decoration: changing their order breaks what the mark says.

## The wallpaper

The same table at desktop scale. The three system partitions terminate at their real
relative widths; root runs off the right edge through a long dissolve, which is the 8 GB
image growing into whatever drive it was written to. The top-left quadrant is left empty
because Xfce parks desktop icons there, and the wordmark sits bottom right, clear of both
the icons and the panel.

Labels quote real system text in its real casing, never uppercased and letterspaced, and
never a slogan. Uppercasing them also mangles the IEC units into `MIB` and `GIB`, which no
tool prints.

`wallpaper.html` is authored at 1920x1080 and scales its stage to the viewport, so any
target size comes out exact. Deriving sizes from a rounded `--force-device-scale-factor`
instead is what produced the 2559x1440 and 5121x2880 wallpapers that `fb9cec5` had to
replace.

## The boot background

`wallpaper.html` again, with `body.ground` switching off the grid, the partition map and
the wordmark. Sharing the source is the point: the boot screen and the desktop are the
same picture, so the ground cannot drift between them.

Everything with an edge comes off, because GRUB resamples with nearest-neighbour onto
whatever panel the stick is plugged into. That turns the 48px grid into moire and stipples
the bars; a smooth gradient is the one thing that survives it, which is also why the render
is 720p and left for GRUB to stretch. The mark is not in it either -- the boot theme draws
its own, at a size it controls.

It ships as `portlin/resources/grub/background.png` and is named twice, because the boot
sequence has two surfaces. `desktop-image` in `theme.txt` paints the menu; `GRUB_BACKGROUND`
in `/etc/default/grub` paints the terminal screen that replaces the menu once an entry is
chosen, which is where the kernel and initramfs lines are printed. Setting only the theme
leaves that second screen to Debian's `05_debian_theme`, whose fallback chain ends at
desktop-base -- so the effect of leaving it unset is not a bare screen but Debian's
wallpaper behind portlin's boot log.

## Where the mark ships

| Surface | File | Mechanism |
|---|---|---|
| Boot menu | `portlin/resources/grub/logo.png` | GRUB theme in `/boot/grub/themes/portlin`, selected by `GRUB_THEME` |
| Applications menu button | `portlin/resources/runtime/logo.svg` | An icon theme that answers to `org.xfce.panel.applicationsmenu` |
| About Portlin, window list, appfinder | same SVG | Installed into hicolor as `portlin`, named by `Icon=` and `set_default_icon_name` |
| About dialog | same SVG | `/usr/share/portlin/logo.svg`, drawn by `set_logo` |

The boot menu is the one place the mark cannot be an SVG: GRUB has no SVG renderer, so
`grub-logo.html` rasterises it at exactly the size the theme draws it, at 1x and onto
solid ink. GRUB resamples with nearest-neighbour and its own compositor is not a browser's,
so an opaque image at its natural size is the only case that needs neither scaling nor
alpha blending. The theme paints the same ink behind it, which is what makes the square
invisible.

## Reproducing the shipped set

Every wallpaper in `portlin/resources/wallpapers/`, and the boot menu's
`portlin/resources/grub/logo.png` and `background.png`, is rendered by `render.sh` and reproduces byte for byte, so a change to the design can be checked by re-rendering and
diffing. Output still goes to `out/brand/` rather than over the shipped set, so replacing a
wallpaper stays a deliberate act: look at the new renders, then copy them across.
