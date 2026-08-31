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

## Reproducing the shipped set

Every wallpaper in `portlin/resources/wallpapers/` is rendered by `render.sh` and
reproduces byte for byte, so a change to the design can be checked by re-rendering and
diffing. Output still goes to `out/brand/` rather than over the shipped set, so replacing a
wallpaper stays a deliberate act: look at the new renders, then copy them across.
