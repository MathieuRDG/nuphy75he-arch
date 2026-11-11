#!/bin/sh
# Link the tools into ~/.local/bin and add a launcher entry. No root needed.
# --uninstall undoes it all. The udev rule is a separate, one-time sudo step:
# see the README.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$bin/nuphy" "$bin/nuphy-gui" "$apps/nuphy-he.desktop"
    echo "Désinstallé (les sauvegardes restent dans ~/.local/state/nuphy-he)."
    exit 0
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
    echo "Python 3.10 ou plus récent est nécessaire." >&2
    exit 1
}
python3 - <<'PY' || echo "Note : la fenêtre demande GTK 4 et libadwaita (Arch : sudo pacman -S --needed python-gobject gtk4 libadwaita). La ligne de commande marche sans."
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
raise SystemExit(Adw.get_major_version() == 1 and Adw.get_minor_version() < 5)
PY

mkdir -p "$bin" "$apps"
chmod +x "$here/nuphy.py" "$here/nuphy-gui.py"
ln -sf "$here/nuphy.py" "$bin/nuphy"
ln -sf "$here/nuphy-gui.py" "$bin/nuphy-gui"

cat > "$apps/nuphy-he.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Clavier NuPhy HE
Comment=Touches, point d'actionnement, rapid trigger, éclairage et macros du clavier NuPhy
Exec=python3 $here/nuphy-gui.py
Icon=input-keyboard
Categories=Settings;HardwareSettings;
Keywords=keyboard;clavier;nuphy;air75;rapid trigger;
DESKTOP

echo "Installé : « nuphy » en ligne de commande, « Clavier NuPhy HE » dans le lanceur."
if [ ! -e /etc/udev/rules.d/70-nuphy-he.rules ]; then
    echo "Reste la règle udev (une fois, avec sudo) :"
    echo "  sudo install -m 644 $here/70-nuphy-he.rules /etc/udev/rules.d/"
    echo "  sudo udevadm control --reload && sudo udevadm trigger"
fi
case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "Note : $bin n'est pas dans le PATH (fish : fish_add_path ~/.local/bin)." ;;
esac
