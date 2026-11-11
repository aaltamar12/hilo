#!/usr/bin/env bash
# Instala Hilo para el usuario actual.
#   ./install.sh              comando `hilo` + acceso en el menú de aplicaciones
#   ./install.sh --autostart  además la abre al iniciar sesión (recupera la sesión tras un apagón)
#   ./install.sh --uninstall
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/hilo"
DESK="$HOME/.local/share/applications/dev.hilo.Terminal.desktop"
AUTO="$HOME/.config/autostart/dev.hilo.Terminal.desktop"
ICONS="$HOME/.local/share/icons/hicolor"
SIZES="16 24 32 48 64 128 256 512"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$BIN" "$DESK" "$AUTO" "$ICONS/scalable/apps/dev.hilo.Terminal.svg"
  for s in $SIZES; do rm -f "$ICONS/${s}x${s}/apps/dev.hilo.Terminal.png"; done
  gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
  echo "Hilo desinstalado (la sesión guardada sigue en ~/.local/state/hilo)."
  exit 0
fi

python3 -c "import gi; gi.require_version('Vte','2.91'); from gi.repository import Vte" 2>/dev/null || {
  echo "Falta VTE para Python: sudo apt install gir1.2-vte-2.91 python3-gi"; exit 1; }

chmod +x "$DIR/hilo.py"
mkdir -p "$(dirname "$BIN")" "$(dirname "$DESK")"
ln -sf "$DIR/hilo.py" "$BIN"
cat > "$DESK" <<DESKTOP
[Desktop Entry]
Name=Hilo
GenericName=Terminal
Comment=Terminal que no pierde el hilo: recupera sus pestañas y sesiones de Claude tras un apagado
Keywords=terminal;consola;shell;claude;sesion;
Exec=$BIN
Icon=dev.hilo.Terminal
Terminal=false
Type=Application
Categories=System;TerminalEmulator;
StartupWMClass=dev.hilo.Terminal
StartupNotify=true
Actions=new-window;new-fresh;

[Desktop Action new-window]
Name=Nueva ventana
Exec=$BIN

[Desktop Action new-fresh]
Name=Abrir sin restaurar
Exec=$BIN --fresh
DESKTOP

if [[ "${1:-}" == "--autostart" ]]; then
  mkdir -p "$(dirname "$AUTO")"
  cp "$DESK" "$AUTO"
  echo "Se abrirá sola al iniciar sesión."
fi
# Icono: SVG y PNG en varios tamaños (el dock y Alt+Tab usan los PNG)
mkdir -p "$ICONS/scalable/apps"
cp "$DIR/icons/dev.hilo.Terminal.svg" "$ICONS/scalable/apps/"
python3 - "$DIR/icons/dev.hilo.Terminal.svg" "$ICONS" $SIZES <<'PY'
import os, sys, gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
svg, base, *sizes = sys.argv[1:]
for s in map(int, sizes):
    d = f"{base}/{s}x{s}/apps"
    os.makedirs(d, exist_ok=True)
    GdkPixbuf.Pixbuf.new_from_file_at_size(svg, s, s).savev(f"{d}/dev.hilo.Terminal.png", "png", [], [])
PY
touch "$ICONS"
gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
update-desktop-database "$(dirname "$DESK")" 2>/dev/null || true
echo "Listo: ejecute 'hilo' o búsquela como Hilo en el menú."
