# Sourced by /etc/X11/Xsession.d. Part of portlin-desktop.
#
# Portlin's system defaults live in a directory portlin owns rather than at the
# canonical /etc/xdg locations, because dpkg lets exactly one installed package
# own a path and xfce4-settings already ships xsettings.xml. Adding that
# directory to XDG_CONFIG_DIRS is the whole reason the defaults take effect.
#
# The second step matters as much as the first. xfconfd is a D-Bus activated
# user service, so it is started from the activation environment and never sees
# a variable that was only exported into this shell. Debian's own
# 55xfce4-session performs the same dance for XDG_DATA_DIRS.

if [ -d /etc/xdg/xdg-portlin ]; then
    # An unset XDG_CONFIG_DIRS means /etc/xdg, so the default has to be spelled
    # out here; appending to nothing would drop it off the search path.
    XDG_CONFIG_DIRS="/etc/xdg/xdg-portlin:${XDG_CONFIG_DIRS:-/etc/xdg}"
    export XDG_CONFIG_DIRS

    if [ -n "$DBUS_SESSION_BUS_ADDRESS" ] &&
        command -v dbus-update-activation-environment >/dev/null; then
        dbus-update-activation-environment --systemd XDG_CONFIG_DIRS
    fi
fi
