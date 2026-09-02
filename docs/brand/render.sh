#!/usr/bin/env bash
# Rebuild every brand asset from the sources in this directory.
#
# Rasterising through headless Chrome rather than a standalone SVG converter is
# deliberate: Chrome resolves system-installed fonts, so the Futura wordmark and the
# Menlo labels render as real type instead of needing to be converted to paths first.
#
# Output goes to out/brand/, never straight into portlin/resources/wallpapers/. The
# shipped wallpapers are reviewed before they are replaced.
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-../../out/brand}"
mkdir -p "$OUT"

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[ -x "$CHROME" ] || { echo "chrome not found at: $CHROME" >&2; exit 1; }

shot() {  # page scale WxH out
    "$CHROME" --headless --disable-gpu --hide-scrollbars \
        --default-background-color=00000000 \
        --force-device-scale-factor="$2" --window-size="$3" \
        --screenshot="$OUT/$4" "file://$PWD/$1" >/dev/null 2>&1
}

# The lockup is one source with two colourways, picked by a class on <body>, so the
# geometry cannot drift between the light and dark versions.
{ echo '<body class="light">'; cat lockup.html; } > .lockup-light.html
trap 'rm -f .lockup-light.html' EXIT

shot logo.html         2 256,256 portlin-logo.png
# 1x and opaque, unlike every other asset here: this one is decoded by GRUB's
# png module at boot, and drawn at exactly this size by the boot theme.
shot grub-logo.html    1 128,128 portlin-grub-logo.png
shot lockup.html       2 524,172 portlin-lockup-dark.png
shot .lockup-light.html 2 524,172 portlin-lockup-light.png

# wallpaper.html is authored at 1920x1080 and scales its stage to the viewport, so the
# output is exactly the requested size. A rounded device-scale-factor is not: deriving
# 2560x1440 that way lands on 2559x1440.
wallpaper() {  # W H scale windowW windowH
    shot wallpaper.html "$3" "$4,$5" "portlin-$1x$2.png"
}
wallpaper 1365 768  1 1365 768
wallpaper 1920 1080 1 1920 1080
wallpaper 2560 1440 2 1280 720
wallpaper 3840 2160 2 1920 1080
wallpaper 5120 2880 2 2560 1440
wallpaper 7680 4320 2 3840 2160

echo "wrote $(ls "$OUT" | wc -l | tr -d ' ') files to $OUT"
