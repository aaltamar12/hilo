#!/usr/bin/env python3
"""Hilo: terminal basada en VTE (el motor de GNOME Terminal) que guarda
la sesión en disco cada pocos segundos para recuperarla tras un apagado
inesperado: ventanas, pestañas, carpetas, texto en pantalla y el comando
que estaba corriendo (las sesiones de Claude Code se reanudan con --resume).
"""
import hashlib
import json
import os
import shlex
import signal
import sys
import termios
import time
import uuid

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

# HILO_APP_ID permite correr una copia de desarrollo al lado de la Hilo instalada.
APP_ID = os.environ.get("HILO_APP_ID", "dev.hilo.Terminal")
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.join(HOME, ".local/state")), "hilo")
SCROLL_DIR = os.path.join(STATE_DIR, "scroll")
STATE_FILE = os.path.join(STATE_DIR, "session.json")
CONFIG_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "hilo", "config.json")
CLAUDE_SESSIONS = os.path.join(HOME, ".claude", "sessions")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
HISTORY_FILE = os.path.join(STATE_DIR, "claude-historial.json")
CLOSED_DIR = os.path.join(STATE_DIR, "cerradas")
HISTORY_MAX = 300
CLOSED_MAX = 40

DEFAULT_CONFIG = {
    # Reanudar Claude Code automáticamente (si no, solo se escribe el comando).
    "autorun_claude": True,
    # Comandos que se vuelven a ejecutar solos; el resto queda escrito esperando Enter.
    "autorun": ["nvim", "vim", "htop", "btop", "top", "less", "man", "tail"],
    "state_interval_s": 2,
    "scroll_interval_s": 10,
    # Mostrar RAM y CPU de cada pestaña (shell + todo lo que corre dentro).
    "show_usage": True,
    # CPU, temperatura y RAM de todo el equipo, al final de la barra de pestañas.
    "show_system": True,
    # Modo discreto: la barra de pestañas oculta (se siguen manejando con el teclado).
    "tabs_hidden": False,
    # Idioma de la interfaz: "auto" (el del sistema), "es" o "en".
    "language": "auto",
    # Lectura en voz alta: voz de piper (vacío = la primera en ~/.local/share/piper-tts)
    # y velocidad (menos = más rápido).
    "tts_model": "",
    "tts_speed": 1.0,
    # Los atajos se cambian en Preferencias (Ctrl+,); quedan en "shortcuts".
}

PCRE2_CASELESS = 0x00000008
PCRE2_MULTILINE = 0x00000400
URL_RE = r"(https?|ftp|file)://[^\s<>\"'`)\]]+"
CLAUDE_VALUE_FLAGS = {
    "--model", "--permission-mode", "--add-dir", "--agent", "--settings",
    "--mcp-config", "--allowedTools", "--allowed-tools", "--disallowedTools",
    "--disallowed-tools", "--append-system-prompt", "--system-prompt", "--fallback-model",
}
CLAUDE_DROP_FLAGS = {"-c", "--continue", "-p", "--print"}
CLAUDE_DROP_VALUE_FLAGS = {"-r", "--resume", "--session-id"}


# ---------------------------------------------------------------- idioma
# Los textos se escriben en español y T() los traduce. "language" en la
# configuración: "auto" (el del sistema), "es" o "en". Se decide al arrancar;
# cambiarlo en Preferencias hace una actualización en caliente, sin cerrar nada.

def ui_language():
    try:
        with open(CONFIG_FILE) as f:
            pref = json.load(f).get("language", "auto")
    except (OSError, ValueError, AttributeError):
        pref = "auto"
    if pref in ("es", "en"):
        return pref
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v and v not in ("C", "POSIX"):
            return "es" if v.split(":")[0].startswith("es") else "en"
    return "en"


LANG = ui_language()

EN = {
    # acciones
    "Nueva pestaña": "New tab",
    "Nueva ventana": "New window",
    "Dividir panel a la derecha": "Split pane right",
    "Dividir panel hacia abajo": "Split pane down",
    "Cerrar panel (o la pestaña si es el único)": "Close pane (or the tab if it's the only one)",
    "Maximizar / restaurar panel": "Maximize / restore pane",
    "Mover panel a una pestaña nueva": "Move pane to a new tab",
    "Ir al panel de la izquierda": "Go to the pane on the left",
    "Ir al panel de la derecha": "Go to the pane on the right",
    "Ir al panel de arriba": "Go to the pane above",
    "Ir al panel de abajo": "Go to the pane below",
    "Cerrar pestaña completa": "Close the whole tab",
    "Cerrar ventana": "Close window",
    "Copiar": "Copy",
    "Pegar": "Paste",
    "Buscar": "Search",
    "Pestaña siguiente": "Next tab",
    "Pestaña anterior": "Previous tab",
    "Mover pestaña a la derecha": "Move tab right",
    "Mover pestaña a la izquierda": "Move tab left",
    "Renombrar pestaña": "Rename tab",
    "Agrandar texto": "Larger text",
    "Achicar texto": "Smaller text",
    "Tamaño normal": "Normal size",
    "Ocultar / mostrar las pestañas": "Hide / show tabs",
    "Pantalla completa": "Full screen",
    "Preferencias": "Preferences",
    "Ver atajos y controles": "Show shortcuts and controls",
    "Aplicar actualización de Hilo sin cerrar nada": "Apply Hilo update without closing anything",
    # ventana de atajos y controles
    "Pestañas": "Tabs",
    "Paneles": "Panes",
    "Texto": "Text",
    "Ventana": "Window",
    "Ir a la pestaña 1 a 9": "Go to tab 1 to 9",
    "Seleccionar texto": "Select text",
    "Arrastrar con el mouse": "Drag with the mouse",
    "Seleccionar dentro de un programa que usa el mouse (Claude, vim)":
        "Select inside a program that uses the mouse (Claude, vim)",
    "Shift + arrastrar": "Shift + drag",
    "Seleccionar palabra / línea": "Select word / line",
    "Doble clic / triple clic": "Double click / triple click",
    "Abrir un enlace": "Open a link",
    "Ctrl + clic": "Ctrl + click",
    "Menú del panel": "Pane menu",
    "Clic derecho": "Right click",
    "Doble clic en la pestaña": "Double click the tab",
    "Volver una pestaña panel": "Turn a tab into a pane",
    "Arrastrar la pestaña sobre la terminal": "Drag the tab onto the terminal",
    "Pasar una pestaña a otra ventana": "Move a tab to another window",
    "Arrastrarla a la otra ventana": "Drag it to the other window",
    "Abrir una pestaña en ventana nueva": "Open a tab in a new window",
    "Soltarla fuera de toda ventana": "Drop it outside any window",
    "Cambiar el tamaño de los paneles": "Resize panes",
    "Arrastrar la línea divisoria": "Drag the divider",
    "Atajos y controles": "Shortcuts and controls",
    "Cambiar atajos…": "Change shortcuts…",
    " o ": " or ",
    "sin atajo": "no shortcut",
    "Teclas fijas y mouse": "Fixed keys and mouse",
    # panel, pestaña y ventana
    "sesión restaurada {when}": "session restored {when}",
    "No se pudo abrir la shell: {error}": "Could not start the shell: {error}",
    "Hay un proceso corriendo en este panel.": "A process is running in this pane.",
    "Cerrar panel": "Close pane",
    "Cerrar pestaña (todos sus paneles)": "Close tab (all its panes)",
    "Hay un proceso corriendo en esta pestaña.": "A process is running in this tab.",
    "Hay {n} procesos corriendo en esta pestaña.": "{n} processes are running in this tab.",
    "Cerrar pestaña": "Close tab",
    "Actualizar": "Update",
    "Hay una versión nueva de Hilo. Se aplica sin cerrar nada: "
    "las shells, Claude y lo que esté corriendo siguen vivos.":
        "A new version of Hilo is ready. It applies without closing anything: "
        "shells, Claude and whatever is running keep going.",
    "Todo el equipo: uso de CPU, temperatura del procesador y RAM usada / total":
        "Whole computer: CPU use, processor temperature and used / total RAM",
    "Buscar en el panel (Enter: anterior, Shift+Enter: siguiente)":
        "Search in the pane (Enter: previous, Shift+Enter: next)",
    "Ocultar las pestañas": "Hide tabs",
    "Mostrar las pestañas": "Show tabs",
    "{n} paneles": "{n} panes",
    "Cancelar": "Cancel",
    "Guardar": "Save",
    "Vacío = nombre automático": "Empty = automatic name",
    "Dividir a la derecha": "Split right",
    "Dividir hacia abajo": "Split down",
    "Restaurar paneles": "Restore panes",
    "Maximizar panel": "Maximize pane",
    "Renombrar pestaña…": "Rename tab…",
    "Preferencias…": "Preferences…",
    "Aplicar actualización de Hilo": "Apply Hilo update",
    "Sesiones de Claude": "Claude sessions",
    "Sin sesiones registradas": "No sessions recorded",
    "Paneles cerrados": "Closed panes",
    "No hay paneles cerrados": "No closed panes",
    "Hay un proceso corriendo en esta ventana.": "A process is running in this window.",
    "Hay {n} procesos corriendo en esta ventana.": "{n} processes are running in this window.",
    "Si la cierra, se detienen; la sesión se recuerda para la próxima vez.":
        "If you close it, they stop; the session is remembered for next time.",
    # preferencias
    "Preferencias de Hilo": "Hilo preferences",
    "General": "General",
    "Atajos": "Shortcuts",
    "Mostrar RAM y CPU en las pestañas": "Show RAM and CPU on tabs",
    "La suma de la shell y de todo lo que corre dentro.": "The shell plus everything running inside it.",
    "Mostrar CPU, temperatura y RAM del equipo": "Show the computer's CPU, temperature and RAM",
    "En la barra de pestañas, para todo el equipo.": "In the tab bar, for the whole computer.",
    "Reanudar Claude Code solo al restaurar": "Resume Claude Code on its own when restoring",
    "Si se apaga, el comando queda escrito esperando Enter.":
        "If off, the command is typed and waits for Enter.",
    "Programas que se vuelven a abrir solos": "Programs that reopen on their own",
    "Separados por coma. El resto queda escrito esperando Enter.":
        "Comma separated. Anything else is typed and waits for Enter.",
    "Idioma": "Language",
    "Se aplica al momento, sin cerrar nada.": "Applies right away, without closing anything.",
    "Automático (el del sistema)": "Automatic (system)",
    "La configuración se guarda en {path}": "Settings are saved in {path}",
    "Volver al atajo original": "Restore the default shortcut",
    "Restablecer todos los atajos": "Reset all shortcuts",
    "Haga clic en un atajo y presione la combinación nueva. Esc cancela, Retroceso lo deja sin atajo.":
        "Click a shortcut and press the new combination. Esc cancels, Backspace leaves it with no shortcut.",
    "Presione la combinación…": "Press the combination…",
    "Sin atajo": "No shortcut",
    "También: ": "Also: ",
    "Presione el atajo nuevo para «{action}».": "Press the new shortcut for “{action}”.",
    "{key} se lo quitó a «{action}».": "{key} was taken from “{action}”.",
    # consumo
    "… y {n} procesos más": "… and {n} more processes",
    "Panel {i}": "Pane {i}",
    "Hilo (toda la app): {mem}": "Hilo (whole app): {mem}",
    # errores y línea de comandos
    "La versión nueva de Hilo tiene un error y no se aplicó.":
        "The new version of Hilo has an error and was not applied.",
    "hilo: no se pudo retomar el panel {id}: {e}": "hilo: could not take over pane {id}: {e}",
    "hilo: no se pudo archivar el panel: {e}": "hilo: could not archive the pane: {e}",
    "hilo: no se pudo leer el relevo: {e}": "hilo: could not read the handover file: {e}",
    "hilo: no se pudo guardar la configuración: {e}": "hilo: could not save the settings: {e}",
    "hilo: no se pudo guardar el historial: {e}": "hilo: could not save the history: {e}",
    "hilo: no se pudo guardar el texto: {e}": "hilo: could not save the text: {e}",
    "hilo: no se pudo guardar la sesión: {e}": "hilo: could not save the session: {e}",
    "USO": (
        "Usage: hilo [--fresh | --history [N] | --closed | --update]\n\n"
        "  --fresh        open without restoring the saved session\n"
        "  --update       apply the new version to the running Hilo, without closing anything\n"
        "  --history N    last N Claude sessions with the command to resume them (20)\n"
        "  --closed       saved closed panes\n"
        "  --tts-hook     Claude Code Stop hook: logs each reply to read it aloud\n"
        "\nSession: {state}\nHistory: {history}\nSettings: {config}"),
    "Todavía no hay sesiones de Claude registradas.": "No Claude sessions recorded yet.",
    "abierta": "open",
    "cerrada": "closed",
    "No hay paneles cerrados guardados.": "No saved closed panes.",
    "texto": "text",
    "Hilo no está abierta.": "Hilo is not running.",
    "Listo: Hilo se actualizó sin cerrar nada.": "Done: Hilo updated without closing anything.",
    # arrastrar paneles
    "Espere sobre una pestaña para abrirla · Suelte en un panel para moverlo ahí · "
    "En la barra queda como pestaña · Fuera de la ventana, en una nueva":
        "Hover over a tab to open it · Drop on a pane to move it there · "
        "On the tab bar it becomes a tab · Outside the window, a new one",
    "Espere sobre una pestaña para abrirla · Suelte en el borde de un panel para unirla · "
    "En la barra, para reordenar":
        "Hover over a tab to open it · Drop on the edge of a pane to join it · On the tab bar, to reorder",
    "Arrastrar panel": "Drag pane",
    "Suéltelo en otro panel, en la barra de pestañas o fuera de la ventana":
        "Drop it on another pane, on the tab bar or outside the window",
    "Llevarla a los paneles de otra pestaña": "Take it to another tab's panes",
    "Arrastrarla y esperar sobre esa pestaña": "Drag it and hover over that tab",
    "Sacar un panel o cambiarlo de lugar": "Pull a pane out or move it",
    "Arrastrar el asa ⠿ que aparece arriba del panel": "Drag the ⠿ handle at the top of the pane",
    # lectura en voz alta
    "Lectura en voz alta": "Read aloud",
    "Abrir la lectura en voz alta": "Open read aloud",
    "Lectura: pausar / reanudar": "Read aloud: pause / resume",
    "Lectura: detener y vaciar la cola": "Read aloud: stop and clear the queue",
    "Anterior": "Previous",
    "Siguiente": "Next",
    "Reproducir": "Play",
    "Reproducir la última": "Play the latest",
    "Pausar": "Pause",
    "Reanudar": "Resume",
    "Detener y vaciar la cola": "Stop and clear the queue",
    "Ir a la sesión": "Go to the session",
    "Historial": "History",
    "Borrar": "Clear",
    "Nada sonando": "Nothing playing",
    "Las respuestas nuevas de Claude se leen solas.": "New Claude replies are read on their own.",
    "La lectura está apagada. Elija una respuesta para oírla.": "Reading is off. Pick a reply to hear it.",
    "Leer en voz alta las respuestas nuevas": "Read new replies aloud",
    "Leer en voz alta las respuestas de Claude": "Read Claude's replies aloud",
    "Con piper. El historial y los controles están en el botón del parlante.":
        "With piper. History and controls are under the speaker button.",
    "Todavía no hay respuestas.": "No replies yet.",
    "Claude no está anotando sus respuestas: falta el hook.": "Claude isn't logging its replies: the hook is missing.",
    "Reparar": "Fix",
    "Hook de lectura en voz alta registrado.": "Read-aloud hook registered.",
    "El hook de lectura en voz alta ya estaba registrado.": "The read-aloud hook was already registered.",
    "No se pudo registrar el hook.": "Could not register the hook.",
    "hilo: piper no pudo leer la respuesta {i}": "hilo: piper could not read reply {i}",
}

ES = {
    "USO": (
        "Uso: hilo [--fresh | --historial [N] | --cerradas | --actualizar]\n\n"
        "  --fresh        abrir sin restaurar la sesión guardada\n"
        "  --actualizar   aplicar la versión nueva a la Hilo abierta, sin cerrar nada\n"
        "  --historial N  últimas N sesiones de Claude con su comando para reanudar (20)\n"
        "  --cerradas     paneles cerrados guardados\n"
        "  --tts-hook     hook Stop de Claude Code: anota cada respuesta para leerla en voz alta\n"
        "\nSesión: {state}\nHistorial: {history}\nConfiguración: {config}"),
}


def T(text, **kw):
    """Texto de la interfaz en el idioma elegido; los {campos} se llenan con kw."""
    out = EN.get(text, text) if LANG == "en" else ES.get(text, text)
    return out.format(**kw) if kw else out


def T_count(n, one, many):
    """Singular o plural según n (los textos plurales llevan {n})."""
    return T(one) if n == 1 else T(many, n=n)


def decimal(text):
    """Coma decimal en español, punto en inglés."""
    return text.replace(".", ",") if LANG == "es" else text


# ---------------------------------------------------------------- utilidades

# (acción, descripción, atajos por defecto en formato GTK)
ACTIONS = [
    ("new_tab", T("Nueva pestaña"), ["<Control>t", "<Control><Shift>t"]),
    ("new_window", T("Nueva ventana"), ["<Control><Shift>n"]),
    ("split_right", T("Dividir panel a la derecha"), ["<Control><Shift>e"]),
    ("split_down", T("Dividir panel hacia abajo"), ["<Control><Shift>o"]),
    ("close_pane", T("Cerrar panel (o la pestaña si es el único)"), ["<Control><Shift>w"]),
    ("zoom_pane", T("Maximizar / restaurar panel"), ["<Control><Shift>x"]),
    ("detach_pane", T("Mover panel a una pestaña nueva"), ["<Control><Shift>d"]),
    ("focus_left", T("Ir al panel de la izquierda"), ["<Alt>Left"]),
    ("focus_right", T("Ir al panel de la derecha"), ["<Alt>Right"]),
    ("focus_up", T("Ir al panel de arriba"), ["<Alt>Up"]),
    ("focus_down", T("Ir al panel de abajo"), ["<Alt>Down"]),
    ("close_tab", T("Cerrar pestaña completa"), []),
    ("close_window", T("Cerrar ventana"), ["<Control><Shift>q"]),
    ("copy", T("Copiar"), ["<Control><Shift>c"]),
    ("paste", T("Pegar"), ["<Control><Shift>v"]),
    ("search", T("Buscar"), ["<Control><Shift>f"]),
    ("next_tab", T("Pestaña siguiente"), ["<Control>Page_Down"]),
    ("prev_tab", T("Pestaña anterior"), ["<Control>Page_Up"]),
    ("move_tab_right", T("Mover pestaña a la derecha"), ["<Control><Shift>Page_Down"]),
    ("move_tab_left", T("Mover pestaña a la izquierda"), ["<Control><Shift>Page_Up"]),
    ("rename_tab", T("Renombrar pestaña"), []),
    ("zoom_in", T("Agrandar texto"), ["<Control>plus", "<Control>equal", "<Control>KP_Add", "<Control><Shift>plus"]),
    ("zoom_out", T("Achicar texto"), ["<Control>minus", "<Control>KP_Subtract"]),
    ("zoom_reset", T("Tamaño normal"), ["<Control>0", "<Control>KP_0"]),
    ("toggle_tabs", T("Ocultar / mostrar las pestañas"), ["<Control><Shift>h"]),
    ("fullscreen", T("Pantalla completa"), ["F11"]),
    ("preferences", T("Preferencias"), ["<Control>comma"]),
    ("show_help", T("Ver atajos y controles"), ["<Control><Shift>question"]),
    ("apply_update", T("Aplicar actualización de Hilo sin cerrar nada"), []),
    ("tts_panel", T("Abrir la lectura en voz alta"), []),
    ("tts_play_pause", T("Lectura: pausar / reanudar"), []),
    ("tts_stop", T("Lectura: detener y vaciar la cola"), []),
]

# Cómo se agrupan las acciones en la ventana de atajos y controles.
HELP_GROUPS = [
    (T("Pestañas"), ["new_tab", "close_tab", "next_tab", "prev_tab", "move_tab_right", "move_tab_left",
                  "rename_tab", "toggle_tabs"]),
    (T("Paneles"), ["split_right", "split_down", "focus_left", "focus_right", "focus_up", "focus_down",
                 "zoom_pane", "detach_pane", "close_pane"]),
    (T("Texto"), ["copy", "paste", "search", "zoom_in", "zoom_out", "zoom_reset"]),
    (T("Ventana"), ["new_window", "close_window", "fullscreen", "preferences", "show_help", "apply_update"]),
    (T("Lectura en voz alta"), ["tts_panel", "tts_play_pause", "tts_stop"]),
]
# Lo que no se configura: teclas fijas y gestos con el mouse.
HELP_FIXED = [
    (T("Ir a la pestaña 1 a 9"), "Alt+1 … Alt+9"),
    (T("Seleccionar texto"), T("Arrastrar con el mouse")),
    (T("Seleccionar dentro de un programa que usa el mouse (Claude, vim)"), T("Shift + arrastrar")),
    (T("Seleccionar palabra / línea"), T("Doble clic / triple clic")),
    (T("Abrir un enlace"), T("Ctrl + clic")),
    (T("Menú del panel"), T("Clic derecho")),
    (T("Renombrar pestaña"), T("Doble clic en la pestaña")),
    (T("Volver una pestaña panel"), T("Arrastrar la pestaña sobre la terminal")),
    (T("Llevarla a los paneles de otra pestaña"), T("Arrastrarla y esperar sobre esa pestaña")),
    (T("Sacar un panel o cambiarlo de lugar"), T("Arrastrar el asa ⠿ que aparece arriba del panel")),
    (T("Pasar una pestaña a otra ventana"), T("Arrastrarla a la otra ventana")),
    (T("Abrir una pestaña en ventana nueva"), T("Soltarla fuera de toda ventana")),
    (T("Cambiar el tamaño de los paneles"), T("Arrastrar la línea divisoria")),
]
MODIFIER_KEYS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R",
                 "Super_L", "Super_R", "Hyper_L", "Hyper_R", "ISO_Level3_Shift", "ISO_Level5_Shift",
                 "Caps_Lock", "Num_Lock", "ISO_Next_Group", "ISO_Prev_Group"}
ACTION_LABELS = {a: label for a, label, _ in ACTIONS}
DEFAULT_SHORTCUTS = {a: list(d) for a, _, d in ACTIONS}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    shortcuts = {a: list(d) for a, d in DEFAULT_SHORTCUTS.items()}
    shortcuts.update({a: list(v) for a, v in (cfg.get("shortcuts") or {}).items() if a in shortcuts})
    if not cfg.get("ctrl_t_new_tab", True) and "shortcuts" not in cfg:
        shortcuts["new_tab"] = [x for x in shortcuts["new_tab"] if x != "<Control>t"]
    cfg["shortcuts"] = shortcuts
    return cfg


def save_config(cfg):
    """Guarda solo lo que difiere de los valores por defecto."""
    out = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG and v != DEFAULT_CONFIG[k]}
    sc = {a: v for a, v in cfg["shortcuts"].items() if v != DEFAULT_SHORTCUTS.get(a)}
    if sc:
        out["shortcuts"] = sc
    atomic_write(CONFIG_FILE, json.dumps(out, indent=2, ensure_ascii=False).encode())


def accel_key(accel):
    """'<Control><Shift>t' -> (keyval en minúscula, modificadores)."""
    keyval, mods = Gtk.accelerator_parse(accel)
    if not keyval:
        return None
    return Gdk.keyval_to_lower(keyval), int(mods & Gtk.accelerator_get_default_mod_mask())


def accel_label(accel):
    keyval, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(keyval, mods) if keyval else accel


def atomic_write(path, data: bytes):
    """Escribe sin dejar archivos a medias aunque se vaya la luz."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def read_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def pids_in_group(pgid):
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
            fields = stat[stat.rfind(")") + 2:].split()
            if int(fields[2]) == pgid:
                out.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return out


def claude_session_for(pgid, argv):
    info = claude_session_info(pgid, argv)
    return info["sessionId"] if info else None


def claude_session_info(pgid, argv):
    """Datos de Claude Code (sessionId, name, cwd) para el grupo en primer plano."""
    candidates = [pgid]
    if not os.path.exists(os.path.join(CLAUDE_SESSIONS, f"{pgid}.json")):
        if not any("claude" in a for a in argv[:3]):
            return None
        candidates = pids_in_group(pgid)
    for pid in candidates:
        try:
            with open(os.path.join(CLAUDE_SESSIONS, f"{pid}.json")) as f:
                data = json.load(f)
            if data.get("sessionId"):
                return data
        except (OSError, ValueError):
            continue
    return None


def claude_resume_argv(argv, session_id):
    """Conserva las banderas del claude original y le agrega --resume <id>."""
    out = ["claude"]
    args = argv[1:]
    # Si se lanzó vía node/npx, saltar hasta el primer argumento tras el script de claude.
    for i, a in enumerate(argv):
        if os.path.basename(a).startswith("claude") or a.endswith("cli.js"):
            args = argv[i + 1:]
            break
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a in CLAUDE_DROP_FLAGS:
            i += 1
        elif a in CLAUDE_DROP_VALUE_FLAGS:
            i += 2 if nxt is not None and not nxt.startswith("-") else 1
        elif a.split("=", 1)[0] in CLAUDE_DROP_VALUE_FLAGS:
            i += 1
        elif a in CLAUDE_VALUE_FLAGS and nxt is not None:
            out += [a, nxt]
            i += 2
        elif a.startswith("-"):
            out.append(a)
            i += 1
        else:
            i += 1  # un prompt suelto no se vuelve a enviar
    out += ["--resume", session_id] if session_id else ["--continue"]
    return out


def claude_first_prompt(session_id):
    """Primer mensaje escrito por el usuario en la sesión (para reconocerla)."""
    import glob
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", f"{session_id}.jsonl")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i > 400:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "user" or d.get("isMeta"):
                        continue
                    c = (d.get("message") or {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                    c = " ".join((c or "").split())
                    if c and not c.startswith("<") and not c.startswith("Caveat:"):
                        return c
        except OSError:
            continue
    return None


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def history_label(e, width=70):
    when = time.strftime("%d %b %H:%M", time.localtime(e.get("last_seen") or e.get("started") or 0))
    text = e.get("prompt") or e.get("name") or e["id"][:8]
    label = f"{when} · {pretty_dir(e.get('cwd'))} · {text}"
    return label if len(label) <= width else label[:width - 1] + "…"


def list_closed():
    """Pestañas cerradas archivadas, de la más reciente a la más antigua."""
    out = []
    try:
        names = sorted((n for n in os.listdir(CLOSED_DIR) if n.endswith(".json")), reverse=True)
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(CLOSED_DIR, n)) as f:
                meta = json.load(f)
            meta["_json"] = os.path.join(CLOSED_DIR, n)
            meta["_txt"] = meta["_json"][:-5] + ".txt"
            out.append(meta)
        except (OSError, ValueError):
            continue
    return out


CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024


def scan_procs():
    """{pid: (ppid, ticks de CPU, rss_kb, nombre)} de todos los procesos."""
    out = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                st = f.read()
        except OSError:
            continue
        lp, rp = st.find("("), st.rfind(")")
        f = st[rp + 2:].split()
        try:
            out[int(name)] = (int(f[1]), int(f[11]) + int(f[12]), int(f[21]) * PAGE_KB, st[lp + 1:rp])
        except (ValueError, IndexError):
            continue
    return out


def pss_kb(pid, fallback):
    """PSS reparte la memoria compartida entre procesos: no cuenta doble las librerías."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return fallback


_TEMP_PATH = None


def cpu_temp_path():
    """Sensor de temperatura del procesador (se busca una sola vez)."""
    global _TEMP_PATH
    if _TEMP_PATH is not None:
        return _TEMP_PATH or None
    found = ""
    try:
        for z in sorted(os.listdir("/sys/class/thermal")):
            base = f"/sys/class/thermal/{z}"
            try:
                if open(base + "/type").read().strip() == "x86_pkg_temp":
                    found = base + "/temp"
                    break
            except OSError:
                pass
        if not found:
            for hw in sorted(os.listdir("/sys/class/hwmon")):
                base = f"/sys/class/hwmon/{hw}"
                try:
                    name = open(base + "/name").read().strip()
                except OSError:
                    continue
                if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz") and os.path.exists(base + "/temp1_input"):
                    found = base + "/temp1_input"
                    if name != "acpitz":
                        break
    except OSError:
        pass
    _TEMP_PATH = found
    return found or None


def system_sample(prev):
    """(texto, caliente, estado) con CPU %, temperatura y RAM usada/total del equipo."""
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
    except (OSError, ValueError, IndexError):
        return None, False, prev
    cpu = None
    if prev:
        di, dt = idle - prev[0], total - prev[1]
        cpu = 100.0 * (1 - di / dt) if dt > 0 else 0.0
    parts = [f"CPU {cpu:.0f}%" if cpu is not None else "CPU …"]
    temp = None
    path = cpu_temp_path()
    if path:
        try:
            temp = int(open(path).read()) / 1000
            parts.append(f"{temp:.0f}°C")
        except (OSError, ValueError):
            pass
    tot = mem.get("MemTotal", 0)
    used = tot - mem.get("MemAvailable", mem.get("MemFree", 0))
    parts.append(f"{used / 1048576:.1f}/{tot / 1048576:.1f}G")
    return " · ".join(parts), bool(temp and temp >= 85), (idle, total)


def fmt_mem(kb):
    mb = kb / 1024
    if mb >= 1024:
        return decimal(f"{mb / 1024:.1f} GB")
    return f"{mb:.0f} MB"


def fmt_cpu(pct):
    return f"{pct:.0f} %" if pct >= 10 or pct == 0 else decimal(f"{pct:.1f} %")


def pretty_dir(path):
    if not path:
        return "~"
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~/" + path[len(HOME) + 1:]
    return path


def child_env():
    skip_prefixes = ("CLAUDE", "GNOME_TERMINAL_", "VTE_", "TERM_PROGRAM")
    skip = {"TERM", "COLORTERM", "OLDPWD", "PWD", "SHLVL", "_"}
    env = [f"{k}={v}" for k, v in os.environ.items()
           if k not in skip and not k.startswith(skip_prefixes)]
    env += ["COLORTERM=truecolor", "TERM_PROGRAM=hilo"]
    return env


def user_shell():
    sh = os.environ.get("SHELL")
    if sh and os.access(sh, os.X_OK):
        return sh
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
    except (KeyError, ImportError):
        return "/bin/bash"


def gnome_profile():
    """Fuente, paleta y scrollback del perfil por defecto de GNOME Terminal."""
    prof = {"font": None, "palette": None, "fg": None, "bg": None,
            "use_theme": True, "scrollback": 10000, "cursor": "block"}
    try:
        src = Gio.SettingsSchemaSource.get_default()
        if not src.lookup("org.gnome.Terminal.ProfilesList", True):
            raise LookupError
        pid = Gio.Settings.new("org.gnome.Terminal.ProfilesList").get_string("default")
        p = Gio.Settings.new_with_path(
            "org.gnome.Terminal.Legacy.Profile",
            f"/org/gnome/terminal/legacy/profiles:/:{pid}/")
        if not p.get_boolean("use-system-font"):
            prof["font"] = p.get_string("font")
        prof["palette"] = list(p.get_strv("palette"))
        prof["fg"] = p.get_string("foreground-color")
        prof["bg"] = p.get_string("background-color")
        prof["use_theme"] = p.get_boolean("use-theme-colors")
        prof["scrollback"] = -1 if p.get_boolean("scrollback-unlimited") else p.get_int("scrollback-lines")
        prof["cursor"] = p.get_string("cursor-shape")
    except Exception:
        pass
    if not prof["font"]:
        try:
            prof["font"] = Gio.Settings.new("org.gnome.desktop.interface").get_string("monospace-font-name")
        except Exception:
            prof["font"] = "Monospace 11"
    return prof


def rgba(spec):
    c = Gdk.RGBA()
    return c if spec and c.parse(spec) else None


# ---------------------------------------------------------------- lectura en voz alta

# Un hook Stop de Claude Code anota cada respuesta en TTS_HISTORY (una línea JSON:
# ts, project, text, session_id). Hilo la lee en voz alta con piper y paplay, guarda
# el historial para volver a escucharlo y sabe en qué panel corre cada sesión.
TTS_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local/share")), "claude-tts")
TTS_HISTORY = os.path.join(TTS_DIR, "history.jsonl")
TTS_LOCK = os.path.join(TTS_DIR, "player.lock")
TTS_OFF = os.path.join(HOME, ".claude", "tts-off")       # existe = lectura apagada
PIPER_DIR = os.path.join(HOME, ".local/share/piper-tts")
TTS_KEEP = 200            # líneas del historial que se conservan
TTS_SHOWN = 40            # respuestas que se ven en el panel
TTS_MAX = 700             # caracteres que se leen de cada respuesta
TTS_RATE = 22050
TTS_BPS = TTS_RATE * 2    # s16le mono
TTS_LEAD = 0.25           # audio que paplay tiene por delante (lo que tarda en pausar)


def tts_model(cfg):
    path = os.path.expanduser(cfg.get("tts_model") or "")
    if path and os.path.exists(path):
        return path
    try:
        voices = sorted(f for f in os.listdir(PIPER_DIR) if f.endswith(".onnx"))
    except OSError:
        return None
    return os.path.join(PIPER_DIR, voices[0]) if voices else None


def tts_available(cfg):
    import shutil
    return bool(os.access(os.path.join(PIPER_DIR, "venv/bin/piper"), os.X_OK)
                and tts_model(cfg) and shutil.which("paplay"))


def clean_reply(text):
    """Lo que vale la pena oír: sin bloques de código, enlaces ni marcas de Markdown."""
    import re
    out, fence = [], False
    for line in text.split("\n"):
        if line.startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(line)
    t = "\n".join(out)
    t = re.sub(r"`[^`]*`", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#>|-]+", " ", t)
    return " ".join(t.split())


def tts_hook():
    """`hilo --tts-hook`: hook Stop de Claude Code. Anota la última respuesta."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if data.get("stop_hook_active") or not data.get("transcript_path"):
        return 0
    last = None
    try:
        with open(data["transcript_path"], encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                parts = [c.get("text", "") for c in (d.get("message") or {}).get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text"]
                if "".join(parts):
                    last = "\n".join(parts)
    except OSError:
        return 0
    text = clean_reply(last or "")[:TTS_MAX]
    if not text:
        return 0
    entry = {"ts": int(time.time()), "project": os.path.basename((data.get("cwd") or "").rstrip("/")),
             "text": text, "session_id": data.get("session_id")}
    os.makedirs(TTS_DIR, exist_ok=True)
    with open(TTS_HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return 0


def tts_hook_registered():
    """True si algún hook Stop anota las respuestas; None si settings.json no se puede leer ahora."""
    try:
        with open(os.path.join(HOME, ".claude", "settings.json"), encoding="utf-8") as f:
            stops = (json.load(f).get("hooks") or {}).get("Stop") or []
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    cmds = [h.get("command") or "" for g in stops for h in g.get("hooks") or []]
    return any("--tts-hook" in c for c in cmds)


# Hooks del antiguo Claude TTS (icono de bandeja): Hilo los reemplaza.
OLD_TTS_HOOKS = ("speak-last-reply.sh", "tts-note-tab.sh")


def tts_register_hook():
    """Deja `hilo --tts-hook` como hook Stop, en un grupo propio (otras herramientas, como
    Roam, reescriben los grupos que tienen comandos suyos), y quita los del antiguo Claude
    TTS. Devuelve True si cambió algo; antes guarda una copia en settings.json.bak-hilo."""
    path = os.path.join(HOME, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        settings = json.loads(raw)
    except FileNotFoundError:
        raw, settings = None, {}
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            keep = [h for h in g.get("hooks") or [] if not any(o in (h.get("command") or "") for o in OLD_TTS_HOOKS)]
            changed |= len(keep) != len(g.get("hooks") or [])
            if keep:
                groups.append(dict(g, hooks=keep))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    stops = hooks.setdefault("Stop", [])
    if not any("--tts-hook" in (h.get("command") or "") for g in stops for h in g.get("hooks") or []):
        exe = os.path.join(HOME, ".local/bin/hilo")
        cmd = (exe if os.path.exists(exe) else shlex.quote(os.path.realpath(__file__))) + " --tts-hook"
        stops.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        changed = True
    if changed:
        if raw is not None:
            atomic_write(path + ".bak-hilo", raw.encode())
        atomic_write(path, (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode())
    return changed


class TtsPlayer:
    """Lista de reproducción: el historial. Las respuestas que llegan con la lectura encendida
    quedan "sin oír" y suenan en orden; anterior, siguiente o un clic saltan, y las pendientes
    siguen después. piper sintetiza en un hilo aparte y el audio va a paplay en tiempo real,
    para que la pausa responda al instante."""

    def __init__(self, on_change, model, speed):
        import threading
        self.on_change, self.model, self.speed = on_change, model, speed
        self.entries, self.unplayed = [], []
        self.cursor = -1
        self.state = "idle"          # idle | playing | paused
        self.elapsed = 0.0
        self.enabled = True
        self.duration = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._gen = self._handled = 0
        self._target = None
        self._cache = {}
        self._procs = []
        threading.Thread(target=self._run, daemon=True).start()

    # -- controles (hilo de la interfaz)
    def add(self, new):
        with self._lock:
            base = len(self.entries)
            self.entries.extend(new)
            if self.enabled:
                self.unplayed.extend(range(base, len(self.entries)))
            start = self.unplayed[0] if self.enabled and self.state == "idle" and self.unplayed else None
        if start is not None:
            self.play(start)
        else:
            self.on_change()

    def play(self, index):
        with self._lock:
            self._gen += 1
            self._target = self.cursor = index
            self.state, self.elapsed = "playing", 0.0
            if index in self.unplayed:
                self.unplayed.remove(index)
            self._kill()
        self._wake.set()
        self.on_change()

    def toggle_pause(self):
        if self.state == "playing":
            self.state = "paused"
        elif self.state == "paused":
            self.state = "playing"
        elif self.entries:
            self.play(self.cursor if self.cursor >= 0 else len(self.entries) - 1)
            return
        self.on_change()

    def next(self):
        target = self.unplayed[0] if self.unplayed else (
            self.cursor + 1 if self.cursor + 1 < len(self.entries) else None)
        if target is not None:
            self.play(target)

    def previous(self):
        if self.cursor < 0:
            return
        # Pasados unos segundos, "anterior" vuelve al comienzo de la misma respuesta.
        restart = self.state == "idle" or self.elapsed > 3 or self.cursor == 0
        self.play(self.cursor if restart else self.cursor - 1)

    def stop(self, clear=False):
        with self._lock:
            self._gen += 1
            self._target = None
            self._kill()
            self.unplayed.clear()
            self.state, self.elapsed = "idle", 0.0
            if clear:
                self.entries.clear()
                self._cache.clear()
                self.duration.clear()
                self.cursor = -1
        self.on_change()

    def current(self):
        return self.entries[self.cursor] if 0 <= self.cursor < len(self.entries) else None

    def can_go_next(self):
        return bool(self.unplayed) or self.cursor + 1 < len(self.entries)

    # -- hilo de audio
    def _kill(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                gen, index = self._gen, self._target
            if index is None or gen == self._handled:
                continue
            self._handled = gen
            pcm = self._pcm(gen, index)
            if pcm is None or gen != self._gen:
                continue
            self.duration[index] = len(pcm) / TTS_BPS
            self.on_change()
            if self._stream(gen, pcm):
                self._advance(gen)

    def _pcm(self, gen, index):
        import subprocess
        if index in self._cache:
            return self._cache[index]
        proc = subprocess.Popen(
            [os.path.join(PIPER_DIR, "venv/bin/piper"), "-m", self.model, "--length-scale", str(self.speed),
             "--output-raw"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._procs = [proc]
        try:
            proc.stdin.write(self.entries[index]["text"].encode())
            proc.stdin.close()
            pcm = proc.stdout.read()
        except (OSError, ValueError, IndexError, KeyError):
            pcm = b""
        proc.wait()
        if gen != self._gen:
            return None
        if proc.returncode != 0 or not pcm:
            print(T("hilo: piper no pudo leer la respuesta {i}", i=index), file=sys.stderr)
            self._advance(gen)
            return None
        if len(self._cache) >= 8:
            self._cache.pop(next(iter(self._cache)))
        self._cache[index] = pcm
        return pcm

    def _stream(self, gen, pcm):
        import subprocess
        pa = subprocess.Popen(["paplay", "--raw", f"--rate={TTS_RATE}", "--format=s16le", "--channels=1",
                               "--latency-msec=100"], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._procs = [pa]
        chunk = TTS_BPS // 10
        pos, sent, elapsed, last = 0, 0.0, 0.0, time.monotonic()
        try:
            while gen == self._gen:
                now = time.monotonic()
                dt, last = now - last, now
                if self.state == "paused":
                    time.sleep(0.05)
                    continue
                elapsed += dt
                self.elapsed = elapsed
                if pos < len(pcm):
                    if sent - elapsed > TTS_LEAD:
                        time.sleep(0.03)
                        continue
                    pa.stdin.write(pcm[pos:pos + chunk])
                    pa.stdin.flush()
                    pos += chunk
                    sent = min(pos, len(pcm)) / TTS_BPS
                elif elapsed >= sent + 0.3:
                    return True
                else:
                    time.sleep(0.03)
        except (BrokenPipeError, ValueError, OSError):
            pass
        finally:
            if pa.poll() is None:
                pa.kill()
            pa.wait()
        return False

    def _advance(self, gen):
        with self._lock:
            if gen != self._gen:
                return
            nxt = self.unplayed[0] if self.unplayed and self.enabled else None
            if nxt is None:
                self.state, self.elapsed = "idle", 0.0
        if nxt is not None:
            self.play(nxt)
        else:
            self.on_change()


MPRIS_XML = """<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/><property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/><property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
    <method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
    <property name="PlaybackStatus" type="s" access="read"/><property name="Rate" type="d" access="read"/>
    <property name="MinimumRate" type="d" access="read"/><property name="MaximumRate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/><property name="Position" type="x" access="read"/>
    <property name="CanGoNext" type="b" access="read"/><property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/><property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/><property name="CanControl" type="b" access="read"/>
  </interface>
</node>"""


class TtsMpris:
    """Teclas multimedia y el control de medios de GNOME manejan la lectura."""
    PLAYER = "org.mpris.MediaPlayer2.Player"

    def __init__(self, tts):
        self.tts = tts
        self.conn = None
        self.node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        Gio.bus_own_name(Gio.BusType.SESSION, "org.mpris.MediaPlayer2.hilo", Gio.BusNameOwnerFlags.NONE,
                         self.on_bus, None, None)

    def on_bus(self, conn, _name):
        self.conn = conn
        for info in self.node.interfaces:
            conn.register_object("/org/mpris/MediaPlayer2", info, self.on_method, self.on_get, None)

    def props(self, iface):
        p = self.tts.player
        if iface != self.PLAYER:
            return {"CanQuit": GLib.Variant("b", False), "CanRaise": GLib.Variant("b", True),
                    "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "Hilo"),
                    "SupportedUriSchemes": GLib.Variant("as", []), "SupportedMimeTypes": GLib.Variant("as", [])}
        e = p.current()
        meta = {"mpris:trackid": GLib.Variant("o", f"/dev/hilo/tts/{max(p.cursor, 0)}")}
        if e:
            meta["xesam:title"] = GLib.Variant("s", e.get("text", "")[:80])
            meta["xesam:artist"] = GLib.Variant("as", [e.get("project") or "Claude"])
            if p.duration.get(p.cursor):
                meta["mpris:length"] = GLib.Variant("x", int(p.duration[p.cursor] * 1e6))
        return {"PlaybackStatus": GLib.Variant("s", {"playing": "Playing", "paused": "Paused"}.get(p.state, "Stopped")),
                "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
                "MaximumRate": GLib.Variant("d", 1.0), "Metadata": GLib.Variant("a{sv}", meta),
                "Position": GLib.Variant("x", int(p.elapsed * 1e6)),
                "CanGoNext": GLib.Variant("b", p.can_go_next()), "CanGoPrevious": GLib.Variant("b", p.cursor >= 0),
                "CanPlay": GLib.Variant("b", bool(p.entries)), "CanPause": GLib.Variant("b", True),
                "CanSeek": GLib.Variant("b", False), "CanControl": GLib.Variant("b", True)}

    def on_get(self, _c, _s, _p, iface, name):
        return self.props(iface)[name]

    def on_method(self, _c, _s, _p, iface, method, _params, inv):
        p = self.tts.player
        fn = {"Next": p.next, "Previous": p.previous, "PlayPause": p.toggle_pause, "Stop": p.stop,
              "Pause": lambda: p.state == "playing" and p.toggle_pause(),
              "Play": lambda: p.state != "playing" and p.toggle_pause(),
              "Raise": lambda: self.tts.app.windows and self.tts.app.windows[0].present()}.get(method)
        if fn:
            fn()
        inv.return_value(None)

    def notify(self):
        if self.conn:
            changed = {k: v for k, v in self.props(self.PLAYER).items() if k != "Position"}
            self.conn.emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged", GLib.Variant("(sa{sv}as)", (self.PLAYER, changed, [])))


class Tts:
    """Lee el historial que escribe el hook, maneja el reproductor y sabe de qué panel vino
    cada respuesta. Solo una Hilo a la vez lee en voz alta (candado en TTS_LOCK)."""

    def __init__(self, app, handover=None):
        self.app = app
        self.owner = False
        self.lock_fd = None
        self.offset = 0
        self.mpris = None
        self.enabled = not os.path.exists(TTS_OFF)
        self.player = TtsPlayer(lambda: GLib.idle_add(self.changed), tts_model(app.config),
                                app.config.get("tts_speed", 1.0))
        self.load(handover)
        self.take_lock((handover or {}).get("lock_fd"))
        self.player.enabled = self.enabled and self.owner
        GLib.timeout_add(500, self.poll)

    def take_lock(self, inherited=None):
        """El candado viaja abierto en una actualización en caliente (como los pty):
        si se soltara un instante, otra Hilo podría quedarse con la lectura."""
        import fcntl
        if inherited is not None:
            try:
                fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.set_inheritable(inherited, False)
                self.lock_fd, self.owner = inherited, True
                self.mpris = TtsMpris(self)
                return True
            except OSError:
                pass
        try:
            os.makedirs(TTS_DIR, exist_ok=True)
            fd = os.open(TTS_LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        except OSError:
            return False
        self.lock_fd, self.owner = fd, True
        self.mpris = TtsMpris(self)
        self.changed()
        return True

    def load(self, handover):
        """Lo que ya estaba anotado es historial: no se lee solo, salvo la cola que
        traía la versión anterior de Hilo en una actualización en caliente."""
        try:
            with open(TTS_HISTORY, "rb") as f:
                data = f.read()
        except OSError:
            return
        if handover and 0 <= handover.get("offset", -1) <= len(data):
            data = data[:handover["offset"]]
        complete = data[:data.rfind(b"\n") + 1]
        lines = complete.decode("utf-8", "replace").splitlines()
        if not handover and len(lines) > 2 * TTS_KEEP:
            lines = lines[-TTS_KEEP:]
            complete = ("\n".join(lines) + "\n").encode()
            try:
                atomic_write(TTS_HISTORY, complete)
            except OSError:
                pass
        self.offset = len(complete)
        p = self.player
        p.entries = self.parse(complete)
        p.cursor = len(p.entries) - 1
        if handover:
            n = len(p.entries)
            p.unplayed = [i for i in handover.get("unplayed", []) if 0 <= i < n]
            cur = handover.get("cursor", -1)
            if 0 <= cur < n:
                p.cursor = cur
                if handover.get("state") == "playing":
                    GLib.idle_add(lambda: self.owner and p.play(cur) and False)

    @staticmethod
    def parse(raw):
        out = []
        for line in raw.decode("utf-8", "replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("text"):
                out.append(e)
        return out

    def handover(self):
        p = self.player
        out = {"offset": self.offset, "cursor": p.cursor, "unplayed": list(p.unplayed), "state": p.state}
        if self.lock_fd is not None:
            os.set_inheritable(self.lock_fd, True)
            out["lock_fd"] = self.lock_fd
        return out

    def poll(self):
        if self.app.frozen:
            return True
        if not self.owner and self.take_lock():
            self.player.enabled = self.enabled
        enabled = not os.path.exists(TTS_OFF)
        if enabled != self.enabled:
            self.enabled = enabled
            self.player.enabled = enabled and self.owner
            if not enabled:
                self.player.stop()
            self.changed()
        try:
            size = os.path.getsize(TTS_HISTORY)
        except OSError:
            size = 0
        if size < self.offset:
            self.offset = size
        elif size > self.offset:
            with open(TTS_HISTORY, "rb") as f:
                f.seek(self.offset)
                data = f.read()
            complete = data[:data.rfind(b"\n") + 1]
            self.offset += len(complete)
            new = self.parse(complete)
            if new:
                self.player.add(new)
        if self.player.state == "playing":
            for w in self.app.windows:
                w.update_tts(progress_only=True)
        return True

    def changed(self):
        for w in self.app.windows:
            w.update_tts()
            w.refresh_titles()
        if self.mpris:
            self.mpris.notify()
        return False

    def set_enabled(self, on):
        try:
            if on and os.path.exists(TTS_OFF):
                os.remove(TTS_OFF)
            elif not on:
                os.makedirs(os.path.dirname(TTS_OFF), exist_ok=True)
                open(TTS_OFF, "a").close()
        except OSError:
            pass
        self.poll()

    def clear(self):
        self.player.stop(clear=True)
        try:
            atomic_write(TTS_HISTORY, b"")
        except OSError:
            pass
        self.offset = 0

    def speaking(self):
        return self.player.current() if self.player.state != "idle" else None

    def pane_for(self, entry):
        """El panel donde corre (o corrió) la sesión de Claude de esa respuesta."""
        if not entry:
            return None
        sid = entry.get("session_id")
        panes = self.app.all_panes()
        if sid:
            hit = next((p for p in panes if p.live_claude == sid), None) or \
                  next((p for p in panes if sid in p.claude_ids), None)
            if hit:
                return hit
        proj = entry.get("project")
        return next((p for p in panes if p.live_claude and proj
                     and os.path.basename(p.cwd.rstrip("/")) == proj), None)

    def go(self, entry):
        pane = self.pane_for(entry)
        if not pane:
            return
        tab, win = pane.tab, pane.win
        if tab.zoomed and tab.zoomed is not pane:
            tab.unzoom()
        win.nb.set_current_page(win.nb.page_num(tab.widget))
        tab.set_focused(pane)
        win.present()
        GLib.idle_add(lambda: pane.term.grab_focus() and False)

    def shutdown(self):
        self.player.stop()


def fmt_secs(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


class TtsPanel(Gtk.Popover):
    """Controles e historial de la lectura en voz alta, bajo el botón del parlante."""

    def __init__(self, win):
        super().__init__(relative_to=win.tts_btn, position=Gtk.PositionType.BOTTOM)
        self.win, self.app = win, win.app
        self.sig = None
        self.get_style_context().add_class("hilo-tts-panel")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
        box.set_size_request(420, -1)

        self.warn = Gtk.Box(spacing=10)
        wl = Gtk.Label(label=T("Claude no está anotando sus respuestas: falta el hook."),
                       xalign=0, wrap=True, max_width_chars=34)
        fix = Gtk.Button(label=T("Reparar"), valign=Gtk.Align.CENTER)
        fix.connect("clicked", self.on_fix)
        self.warn.pack_start(wl, True, True, 0)
        self.warn.pack_end(fix, False, False, 0)
        self.warn.get_style_context().add_class("hilo-tts-warn")
        box.pack_start(self.warn, False, False, 0)

        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.text = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                              width_chars=46, max_width_chars=46)
        self.text.get_style_context().add_class("dim-label")
        box.pack_start(self.title, False, False, 0)
        box.pack_start(self.text, False, False, 0)
        prog = Gtk.Box(spacing=10)
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.bar.get_style_context().add_class("hilo-tts")
        self.time = Gtk.Label()
        self.time.get_style_context().add_class("dim-label")
        self.time.get_style_context().add_class("hilo-usage")
        prog.pack_start(self.bar, True, True, 0)
        prog.pack_end(self.time, False, False, 0)
        box.pack_start(prog, False, False, 0)

        p = self.app.tts.player
        ctl = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)

        def btn(icon, tip, fn):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_: fn())
            ctl.pack_start(b, False, False, 0)
            return b
        self.prev_b = btn("media-skip-backward-symbolic", T("Anterior"), p.previous)
        self.play_b = btn("media-playback-start-symbolic", T("Reproducir"), p.toggle_pause)
        self.next_b = btn("media-skip-forward-symbolic", T("Siguiente"), p.next)
        self.stop_b = btn("media-playback-stop-symbolic", T("Detener y vaciar la cola"), p.stop)
        self.go_b = btn("go-jump-symbolic", T("Ir a la sesión"), lambda: self.go(p.current()))
        box.pack_start(ctl, False, False, 0)

        sw_row = Gtk.Box(spacing=10)
        sw_row.pack_start(Gtk.Label(label=T("Leer en voz alta las respuestas nuevas"), xalign=0), True, True, 0)
        self.sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.sw_handler = self.sw.connect("notify::active", lambda s, _p: self.app.tts.set_enabled(s.get_active()))
        sw_row.pack_end(self.sw, False, False, 0)
        box.pack_start(sw_row, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)

        head = Gtk.Box(spacing=10)
        hl = Gtk.Label(xalign=0)
        hl.set_markup(f"<b>{GLib.markup_escape_text(T('Historial'))}</b>")
        head.pack_start(hl, True, True, 0)
        self.wipe = Gtk.Button(label=T("Borrar"))
        self.wipe.set_relief(Gtk.ReliefStyle.NONE)
        self.wipe.connect("clicked", lambda *_: self.app.tts.clear())
        head.pack_end(self.wipe, False, False, 0)
        box.pack_start(head, False, False, 0)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.list.get_style_context().add_class("frame")
        self.list.connect("row-activated", lambda _l, row: p.play(row.index))
        self.empty = Gtk.Label(label=T("Todavía no hay respuestas."), margin=14)
        self.empty.get_style_context().add_class("dim-label")
        self.list.set_placeholder(self.empty)
        self.empty.show()
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                max_content_height=340)
        sc.add(self.list)
        box.pack_start(sc, True, True, 0)
        self.add(box)
        box.show_all()
        self.connect("show", lambda *_: (self.warn.set_visible(tts_hook_registered() is False),
                                         self.refresh(force=True)))

    def on_fix(self, *_):
        try:
            tts_register_hook()
        except (OSError, ValueError) as e:
            self.app.show_error(T("No se pudo registrar el hook."), str(e))
        self.warn.set_visible(tts_hook_registered() is False)

    def go(self, entry):
        self.popdown()
        self.app.tts.go(entry)

    def refresh(self, force=False, progress_only=False):
        tts = self.app.tts
        p = tts.player
        dur = p.duration.get(p.cursor) or 0
        if p.state != "idle":
            self.bar.set_fraction(min(p.elapsed / dur, 1.0) if dur else 0.0)
            self.time.set_text(f"{fmt_secs(p.elapsed)} / {fmt_secs(dur)}" if dur else "…")
        else:
            self.bar.set_fraction(0.0)
            self.time.set_text("")
        if progress_only and not force:
            return
        cur = p.current()
        if cur and p.state != "idle":
            self.title.set_markup(f"<b>{GLib.markup_escape_text(cur.get('project') or 'Claude')}</b>")
            self.text.set_text(cur.get("text", ""))
        else:
            self.title.set_markup(f"<b>{GLib.markup_escape_text(T('Nada sonando'))}</b>")
            self.text.set_text(T("Las respuestas nuevas de Claude se leen solas.") if tts.enabled
                               else T("La lectura está apagada. Elija una respuesta para oírla."))
        playing = p.state == "playing"
        self.play_b.get_image().set_from_icon_name(
            "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        self.play_b.set_tooltip_text(T("Pausar") if playing else T("Reanudar") if p.state == "paused"
                                     else T("Reproducir la última"))
        self.play_b.set_sensitive(bool(p.entries))
        self.prev_b.set_sensitive(p.cursor >= 0)
        self.next_b.set_sensitive(p.can_go_next())
        self.stop_b.set_sensitive(p.state != "idle" or bool(p.unplayed))
        self.go_b.set_sensitive(tts.pane_for(cur) is not None)
        self.wipe.set_sensitive(bool(p.entries))
        self.sw.handler_block(self.sw_handler)
        self.sw.set_active(tts.enabled)
        self.sw.handler_unblock(self.sw_handler)

        sig = (len(p.entries), p.cursor, p.state, len(self.app.all_panes()))
        if sig == self.sig and not force:
            return
        self.sig = sig
        for row in self.list.get_children():
            self.list.remove(row)
        for i in range(len(p.entries) - 1, max(len(p.entries) - 1 - TTS_SHOWN, -1), -1):
            e = p.entries[i]
            row = Gtk.ListBoxRow()
            row.index = i
            h = Gtk.Box(spacing=8, margin=6, margin_start=10)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            on = i == p.cursor and p.state != "idle"
            meta = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            when = time.strftime("%H:%M", time.localtime(e.get("ts") or 0))
            meta.set_markup("<small>" + ("♪  " if on else "") +
                            GLib.markup_escape_text(f"{when} · {e.get('project') or 'Claude'}") + "</small>")
            meta.get_style_context().add_class("hilo-tts-on" if on else "dim-label")
            txt = Gtk.Label(label=e.get("text", ""), xalign=0, ellipsize=Pango.EllipsizeMode.END,
                            width_chars=40, max_width_chars=40)
            v.pack_start(meta, False, False, 0)
            v.pack_start(txt, False, False, 0)
            h.pack_start(v, True, True, 0)
            if tts.pane_for(e):
                g = Gtk.Button.new_from_icon_name("go-jump-symbolic", Gtk.IconSize.MENU)
                g.set_relief(Gtk.ReliefStyle.NONE)
                g.set_valign(Gtk.Align.CENTER)
                g.set_tooltip_text(T("Ir a la sesión"))
                g.connect("clicked", lambda _b, x=e: self.go(x))
                h.pack_end(g, False, False, 0)
            row.add(h)
            row.show_all()
            self.list.add(row)


# ---------------------------------------------------------------- panel (una terminal)

class Pane:
    """Una terminal independiente: su shell, carpeta, comando, texto y consumo."""

    def __init__(self, tab, saved=None):
        self.tab = tab
        self.app = tab.app
        saved = saved or {}
        self.id = saved.get("id") or uuid.uuid4().hex[:12]
        self.cwd = saved.get("cwd") or HOME
        if not os.path.isdir(self.cwd):
            self.cwd = HOME
        self.pid = None
        self.closing = False
        self.cmd = None             # último comando en primer plano (pegajoso)
        self.cmd_gone_since = None
        self.pending_restore = False
        self.pending_input = None
        self.wait_ticks = 0
        self.live_claude = None
        self.claude_ids = list(saved.get("claude_ids") or [])
        self.consume_scroll = False
        self.scroll_dirty = bool(saved.get("id"))
        self.scroll_hash = None
        self.usage = None           # (ram_kb, cpu_pct, filas de procesos)

        t = self.term = Vte.Terminal()
        t.set_scrollback_lines(self.app.profile["scrollback"])
        t.set_scroll_on_keystroke(True)
        t.set_scroll_on_output(False)
        t.set_mouse_autohide(True)
        t.set_allow_hyperlink(True)
        t.set_audible_bell(False)
        t.set_cursor_shape({"ibeam": Vte.CursorShape.IBEAM,
                            "underline": Vte.CursorShape.UNDERLINE}.get(
            self.app.profile["cursor"], Vte.CursorShape.BLOCK))
        self.app.apply_look(t)
        try:
            rx = Vte.Regex.new_for_match(URL_RE, -1, PCRE2_MULTILINE)
            tag = t.match_add_regex(rx, 0)
            t.match_set_cursor_name(tag, "pointer")
        except GLib.Error:
            pass

        t.connect("child-exited", self.on_child_exited)
        t.connect("window-title-changed", lambda *_: self.win.refresh_titles())
        t.connect("contents-changed", self.on_contents_changed)
        t.connect("button-press-event", self.on_button_press)
        t.connect("scroll-event", self.on_scroll)
        self.smooth_dy = 0.0
        t.connect("focus-in-event", lambda *_: self.tab.set_focused(self) or False)

        sb = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=t.get_vadjustment())
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        inner.pack_start(t, True, True, 0)
        inner.pack_start(sb, False, False, 0)
        # El marco solo se ve con varios paneles: resalta el que tiene el foco.
        self.widget = Gtk.Frame(shadow_type=Gtk.ShadowType.NONE)
        self.widget.get_style_context().add_class("hilo-pane")
        # Asa para arrastrar el panel: aparece al acercar el mouse al borde de arriba,
        # solo si la pestaña tiene varios paneles.
        self.grip = Gtk.EventBox(halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=4, margin_end=22)
        self.grip.set_visible_window(False)
        grip_label = Gtk.Label(label="⠿  " + T("Arrastrar panel"))
        grip_label.get_style_context().add_class("hilo-grip")
        self.grip.add(grip_label)
        self.grip.set_tooltip_text(T("Suéltelo en otro panel, en la barra de pestañas o fuera de la ventana"))
        self.grip.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [PANE_TARGET], Gdk.DragAction.MOVE)
        self.grip.drag_source_set_icon_name("utilities-terminal-symbolic")
        self.grip.connect("drag-begin", lambda *_: self.app.pane_drag_begin(self))
        self.grip.connect("drag-data-get", lambda _w, _c, data, *_: data.set_text(self.id, -1))
        self.grip.connect("drag-failed", self.on_grip_failed)
        self.grip.connect("drag-end", lambda *_: self.app.pane_drag_end())
        self.grip.connect("enter-notify-event", lambda *_: self.set_grip(True, hover=True))
        self.grip.connect("leave-notify-event", lambda *_: self.set_grip(False, hover=False))
        self.grip.connect("realize", lambda w: w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "grab")))
        self.grip_hover = False
        self.grip_on = False
        t.connect("motion-notify-event", self.on_motion)
        t.connect("leave-notify-event", lambda *_: self.set_grip(False) or False)
        # Siempre está puesta, pero invisible y dejando pasar los clics hasta que se acerca el
        # mouse: una capa del Overlay que se muestra después no recibe eventos (GTK no muestra
        # su ventana), así que solo se cambia su opacidad y si deja pasar los clics.
        self.grip_overlay = overlay = Gtk.Overlay()
        overlay.add(inner)
        overlay.add_overlay(self.grip)
        overlay.set_overlay_pass_through(self.grip, True)
        self.grip.set_opacity(0)
        self.widget.add(overlay)
        self.widget.show_all()

        self.relevo = saved.get("relevo")   # terminal viva heredada de la versión anterior
        self.restore_cmd = saved.get("command")
        self.restore_scroll = saved.get("id") and os.path.join(SCROLL_DIR, f"{saved['id']}.txt")
        self.saved_at = saved.get("_saved_at")

    @property
    def win(self):
        return self.tab.win

    # -- arranque
    def start(self):
        if self.relevo and self.adopt():
            return
        if self.restore_scroll and os.path.exists(self.restore_scroll):
            try:
                with open(self.restore_scroll, encoding="utf-8", errors="replace") as f:
                    text = f.read().rstrip()
                if text:
                    when = time.strftime("%d %b %H:%M", time.localtime(self.saved_at)) if self.saved_at else ""
                    self.term.feed((text.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode())
                    self.term.feed(f"\x1b[2m──── {T('sesión restaurada {when}', when=when)} ────\x1b[0m\r\n".encode())
                if self.consume_scroll:
                    for p in (self.restore_scroll, self.restore_scroll[:-4] + ".json"):
                        if os.path.exists(p):
                            os.unlink(p)
            except OSError:
                pass
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, self.cwd, [user_shell()], child_env(),
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self.on_spawned, None)

    def adopt(self):
        """Retoma la terminal que dejó la versión anterior de Hilo (actualización en
        caliente): mismo pty, misma shell y mismos procesos, que nunca se cerraron."""
        r, self.relevo = self.relevo, None
        try:
            pty = Vte.Pty.new_foreign_sync(r["fd"], None)
            self.term.set_pty(pty)
            self.term.watch_child(r["pid"])
        except (GLib.Error, KeyError, TypeError) as e:
            print(T("hilo: no se pudo retomar el panel {id}: {e}", id=self.id, e=e), file=sys.stderr)
            return False
        self.pid = r["pid"]
        self.cmd = self.restore_cmd
        self.restore_cmd = None
        text = r.get("text") or ""
        lines = text.split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            self.term.feed("\r\n".join(lines).encode())
            self.term.feed(f"\r\x1b[{r['col']}C".encode() if r.get("col") else b"\r")
        if r.get("title"):
            self.term.feed(f"\x1b]0;{r['title']}\x07".encode())
        # Los modos de la terminal no viajan: se vuelven a activar los que usaba el
        # programa (pantalla alterna; Claude además mouse y pegado entre corchetes).
        modes = ""
        if r.get("alt"):
            modes += "\x1b[?1049h"
        if r.get("claude"):
            modes += ("\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h" if r.get("alt") else "") + "\x1b[?2004h"
        if modes:
            self.term.feed(modes.encode())
        GLib.timeout_add(400, self.nudge_redraw)
        self.win.refresh_titles()
        return True

    def nudge_redraw(self):
        """SIGWINCH al programa en primer plano para que se vuelva a dibujar."""
        pty = self.term.get_pty()
        try:
            if pty:
                os.killpg(os.tcgetpgrp(pty.get_fd()), signal.SIGWINCH)
        except OSError:
            pass
        return False

    def relevo_info(self):
        """Lo que la versión nueva necesita para retomar esta terminal sin cerrarla."""
        pty = self.term.get_pty()
        if not (pty and self.pid):
            return None
        fd = pty.get_fd()
        os.set_inheritable(fd, True)
        adj = self.term.get_vadjustment()
        alt = adj.get_upper() - adj.get_lower() <= adj.get_page_size() and self.foreground_running()
        fg_argv = []
        try:
            fg = os.tcgetpgrp(fd)
            if fg != self.pid:
                fg_argv = read_cmdline(fg) or []
        except OSError:
            pass
        claude = any(os.path.basename(a) == "claude" for a in fg_argv[:2])
        if alt:
            text = ""      # el programa a pantalla completa se vuelve a dibujar solo
        else:
            text = self.raw_text() or ""
        return {"fd": fd, "pid": self.pid, "alt": alt, "claude": claude,
                "col": self.term.get_cursor_position()[0],
                "title": self.term.get_window_title(), "text": text}

    def raw_text(self):
        stream = Gio.MemoryOutputStream.new_resizable()
        try:
            self.term.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
            stream.close(None)
            return stream.steal_as_bytes().get_data().decode("utf-8", "replace")
        except GLib.Error:
            return None

    def on_spawned(self, term, pid, error, *_):
        if error or pid <= 0:
            term.feed(f"\r\n{T('No se pudo abrir la shell: {error}', error=error)}\r\n".encode())
            return
        self.pid = pid
        rc = self.restore_cmd
        if rc and rc.get("argv"):
            line = shlex.join(rc["argv"])
            name = os.path.basename(rc["argv"][0])
            auto = (rc.get("claude_session") is not None or name == "claude") and self.app.config["autorun_claude"]
            auto = auto or name in self.app.config["autorun"]
            self.cmd = rc
            self.pending_restore = True
            self.pending_input = (line + ("\n" if auto else "")).encode()
            self.wait_ticks = 0
            GLib.timeout_add(100, self.wait_for_line_editor)
        self.restore_cmd = None
        self.win.refresh_titles()

    # -- seguimiento
    def poll(self):
        """Actualiza carpeta y comando en primer plano. Devuelve True si algo cambió."""
        if not self.pid:
            return False
        before = (self.cwd, json.dumps(self.cmd, sort_keys=True))
        cwd = read_cwd(self.pid)
        if cwd:
            self.cwd = cwd
        fg = None
        pty = self.term.get_pty()
        if pty:
            try:
                fg = os.tcgetpgrp(pty.get_fd())
            except OSError:
                fg = None
        now = time.monotonic()
        if fg and fg != self.pid:
            argv = read_cmdline(fg)
            if argv:
                info = claude_session_info(fg, argv)
                sess = info["sessionId"] if info else None
                if self.live_claude and self.live_claude != sess:
                    self.app.claude_ended(self.live_claude)
                    self.live_claude = None
                if sess is not None or any(os.path.basename(a) == "claude" for a in argv[:2]):
                    argv = claude_resume_argv(argv, sess)
                    self.cmd = {"argv": argv, "claude_session": sess}
                    if sess:
                        self.app.claude_seen(sess, argv, info.get("cwd") or self.cwd, info.get("name"), self.id)
                        if sess not in self.claude_ids:
                            self.claude_ids.append(sess)
                        self.live_claude = sess
                else:
                    self.cmd = {"argv": argv, "claude_session": None}
                self.cmd_gone_since = None
                self.pending_restore = False
        elif self.live_claude and fg == self.pid:
            self.app.claude_ended(self.live_claude)
            self.live_claude = None
        if fg == self.pid and self.cmd and not self.pending_restore:
            # Se mantiene unos segundos: si el apagado mata el proceso antes que a
            # nosotros, el último guardado todavía lo recuerda.
            if self.cmd_gone_since is None:
                self.cmd_gone_since = now
            elif now - self.cmd_gone_since > 6:
                self.cmd = None
                self.cmd_gone_since = None
        return before != (self.cwd, json.dumps(self.cmd, sort_keys=True))

    def foreground_running(self):
        pty = self.term.get_pty()
        if not (pty and self.pid):
            return False
        try:
            return os.tcgetpgrp(pty.get_fd()) not in (self.pid, -1)
        except OSError:
            return False

    def title(self):
        wt = self.term.get_window_title()
        if wt:
            return wt
        base = os.path.basename(self.cwd.rstrip("/")) if self.cwd != HOME else "~"
        if self.cmd and self.cmd_gone_since is None and self.cmd.get("argv"):
            return f"{os.path.basename(self.cmd['argv'][0])} · {base}"
        return base or "/"

    def serialize(self):
        d = {"id": self.id, "cwd": self.cwd, "command": self.cmd, "claude_ids": self.claude_ids[-20:]}
        if self.app.relevo_out:
            info = self.relevo_info()
            if info:
                d["relevo"] = info
        return d

    def text(self):
        stream = Gio.MemoryOutputStream.new_resizable()
        try:
            self.term.write_contents_sync(stream, Vte.WriteFlags.DEFAULT, None)
            stream.close(None)
            data = stream.steal_as_bytes().get_data().decode("utf-8", "replace")
        except GLib.Error:
            return None
        # Sin espacios sobrantes al final de cada línea.
        return "\n".join(l.rstrip() for l in data.split("\n"))

    def archive(self):
        """Guarda el texto y los datos del panel al cerrarlo, para poder reabrirlo."""
        if self.app.frozen:
            return
        text = (self.text() or "").strip()
        if not text and not self.claude_ids:
            return
        base = os.path.join(CLOSED_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{self.id}")
        meta = {"id": self.id, "cwd": self.cwd, "title": self.tab.custom_title,
                "closed_at": time.time(), "claude_ids": self.claude_ids,
                "last_line": next((l for l in reversed(text.split("\n")) if l.strip()), "")}
        try:
            atomic_write(base + ".txt", (text + "\n").encode())
            atomic_write(base + ".json", json.dumps(meta, ensure_ascii=False).encode())
            for old in list_closed()[CLOSED_MAX:]:
                for p in (old["_json"], old["_txt"]):
                    if os.path.exists(p):
                        os.unlink(p)
        except OSError as e:
            print(T("hilo: no se pudo archivar el panel: {e}", e=e), file=sys.stderr)

    def dump_scroll(self, force=False):
        if not (self.scroll_dirty or force):
            return
        self.scroll_dirty = False
        text = self.text()
        if text is None:
            return
        blob = (text.rstrip() + "\n").encode()
        h = hashlib.sha1(blob).hexdigest()
        if h == self.scroll_hash:
            return
        self.scroll_hash = h
        atomic_write(os.path.join(SCROLL_DIR, f"{self.id}.txt"), blob)

    # -- eventos
    def on_contents_changed(self, *_):
        self.scroll_dirty = True

    def wait_for_line_editor(self):
        """Escribe el comando pendiente cuando la shell ya activó su editor de
        línea (tty sin ICANON). Así no queda repetido antes del prompt."""
        if not (self.pending_input and self.pid):
            return False
        self.wait_ticks += 1
        ready = self.wait_ticks > 80  # 8 s como máximo
        pty = self.term.get_pty()
        if pty and not ready:
            try:
                ready = not (termios.tcgetattr(pty.get_fd())[3] & termios.ICANON)
            except termios.error:
                ready = True
        if not ready:
            return True
        data, self.pending_input = self.pending_input, None
        self.term.feed_child(data)
        return False

    def on_child_exited(self, *_):
        if self.closing or self.win.closing or self.app.frozen:
            return
        self.pid = None
        # El panel desaparece ya, pero el guardado espera 2 s: si en ese lapso
        # llega un apagado, en disco queda la sesión anterior completa.
        self.app.hold_saves(2.0)
        self.tab.remove_pane(self)

    def on_button_press(self, term, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if event.button == 1 and event.state & Gdk.ModifierType.CONTROL_MASK:
            url = term.hyperlink_check_event(event) if hasattr(term, "hyperlink_check_event") else None
            if not url:
                url, _tag = term.match_check_event(event)
            if url:
                try:
                    Gtk.show_uri_on_window(self.win, url, Gdk.CURRENT_TIME)
                except GLib.Error:
                    pass
                return True
        if event.button == 3:
            term.grab_focus()
            self.win.show_context_menu(self, event)
            return True
        return False

    def on_scroll(self, term, event):
        """Trackpad dentro de programas a pantalla completa (Claude, vim, less).

        VTE 0.68 descarta los desplazamientos suaves pequeños del trackpad
        cuando no hay historial que mover, así que el programa nunca los recibe.
        Se acumulan y se convierten en pasos de rueda normales, que VTE sí le
        pasa al programa. Con historial propio, VTE los maneja como siempre.
        """
        adj = term.get_vadjustment()
        if event.direction != Gdk.ScrollDirection.SMOOTH:
            return False
        if adj.get_upper() - adj.get_lower() > adj.get_page_size():
            self.smooth_dy = 0.0
            return False
        _ok, _dx, dy = event.get_scroll_deltas()
        self.smooth_dy += dy
        steps = int(self.smooth_dy)
        if steps:
            self.smooth_dy -= steps
            direction = Gdk.ScrollDirection.DOWN if steps > 0 else Gdk.ScrollDirection.UP
            for _ in range(abs(steps)):
                e = event.copy()
                e.scroll.direction = direction
                e.scroll.delta_x = e.scroll.delta_y = 0.0
                term.event(e)
        return True

    def on_motion(self, _term, event):
        self.set_grip(event.y < 30)
        return False

    def set_grip(self, show, hover=None):
        if hover is not None:
            self.grip_hover = hover
        multi = len(self.tab.panes) > 1 and not self.tab.zoomed
        visible = multi and (show or self.grip_hover or self.app.dragging_pane is self)
        if visible != self.grip_on:
            self.grip_on = visible
            self.grip.set_opacity(1 if visible else 0)
            self.grip_overlay.set_overlay_pass_through(self.grip, not visible)
        return False

    def on_grip_failed(self, _w, _ctx, result):
        # Soltado fuera de toda ventana: el panel se va a una ventana nueva.
        if result == Gtk.DragResult.NO_TARGET:
            GLib.idle_add(lambda: self.app.pane_to_new_window(self) and False)
            return True
        return False

    def request_close(self):
        """Cierra solo este panel (si es el único, se cierra la pestaña)."""
        if self.foreground_running() and not self.win.confirm(
                T("Hay un proceso corriendo en este panel."), T("Cerrar panel")):
            return
        self.kill()
        self.tab.remove_pane(self, user=True)

    def kill(self):
        self.closing = True
        if self.pid:
            try:
                os.killpg(os.getpgid(self.pid), signal.SIGHUP)
            except OSError:
                pass


# ---------------------------------------------------------------- pestaña (uno o varios paneles)

PANED_RATIOS = {}   # Gtk.Paned -> proporción; también mantiene viva la referencia
TAB_TARGET = Gtk.TargetEntry.new("GTK_NOTEBOOK_TAB", Gtk.TargetFlags.SAME_APP, 0)
PANE_TARGET = Gtk.TargetEntry.new("HILO_PANE", Gtk.TargetFlags.SAME_APP, 1)

class Tab:
    def __init__(self, win, saved=None, adopt=None):
        self.win = win
        self.app = win.app
        saved = saved or {}
        self.id = saved.get("tab_id") or uuid.uuid4().hex[:12]
        self.custom_title = saved.get("title")
        self.panes = []
        self.focused = None
        self.zoomed = None          # panel maximizado dentro de la pestaña
        self.hidden = []
        self.usage = None
        self.drop = None            # (panel, lado) mientras se arrastra una pestaña encima

        self.box = Gtk.Box()
        if adopt:
            # Un panel que se sacó de otra pestaña.
            adopt.tab = self
            self.panes.append(adopt)
            self.box.pack_start(adopt.widget, True, True, 0)
        else:
            layout = saved.get("layout")
            if not layout:
                # Formato de una sola terminal (y sesiones guardadas antes de los paneles).
                layout = {"pane": {k: saved.get(k) for k in ("id", "cwd", "command", "claude_ids")}}
            self.box.pack_start(self.build(layout, saved.get("_saved_at")), True, True, 0)
        # Capa transparente encima para dibujar dónde caerá una pestaña arrastrada.
        self.dropzone = Gtk.DrawingArea()
        # Sin ventana propia: si la tuviera, se quedaría con los clics y el scroll
        # de la terminal de abajo (el pass-through del Overlay no la cubre).
        self.dropzone.set_has_window(False)
        self.dropzone.connect("draw", self.draw_drop)
        self.widget = Gtk.Overlay()
        self.widget.add(self.box)
        self.widget.add_overlay(self.dropzone)
        self.widget.set_overlay_pass_through(self.dropzone, True)
        # Guía que se ve solo mientras se arrastra (con opacidad: una capa del Overlay que
        # se muestra después no se dibuja).
        self.hint = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=12,
                              wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=70)
        self.hint.get_style_context().add_class("hilo-drag-hint")
        self.hint.set_opacity(0)
        self.widget.add_overlay(self.hint)
        self.widget.set_overlay_pass_through(self.hint, True)
        self.spring_frac = None     # 0..1 mientras se espera encima para abrirla
        self.widget.drag_dest_set(Gtk.DestDefaults(0), [TAB_TARGET, PANE_TARGET], Gdk.DragAction.MOVE)
        self.widget.connect("drag-motion", self.on_drag_motion)
        self.widget.connect("drag-leave", lambda *_: self.set_drop(None))
        self.widget.connect("drag-drop", self.on_drag_drop)
        self.widget.show_all()
        focus_idx = saved.get("focused", 0)
        leaves = self.leaf_order()
        self.focused = leaves[focus_idx] if 0 <= focus_idx < len(leaves) else leaves[0]

        # Todas las pestañas piden el mismo ancho natural (el título no cuenta y el
        # consumo tiene ancho fijo), así la barra se reparte por igual y ninguna
        # cambia de tamaño cuando cambian el título o las cifras. El título usa
        # todo el espacio que le toque y solo se corta si no cabe.
        self.label = Gtk.Label(label="…", ellipsize=Pango.EllipsizeMode.END, width_chars=1,
                               max_width_chars=1, hexpand=True)
        self.count_label = Gtk.Label()
        self.count_label.get_style_context().add_class("dim-label")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text(T("Cerrar pestaña (todos sus paneles)"))
        close.connect("clicked", lambda *_: self.request_close())
        self.usage_label = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.END, width_chars=13, max_width_chars=13)
        self.usage_label.get_style_context().add_class("dim-label")
        self.usage_label.get_style_context().add_class("hilo-usage")
        self.tab_box = Gtk.Box(spacing=4)
        self.tab_box.connect("draw", self.draw_spring)
        inner = Gtk.Box(spacing=6)
        inner.pack_start(self.label, True, True, 0)
        inner.pack_start(self.count_label, False, False, 0)
        inner.pack_start(self.usage_label, False, False, 0)
        ev = Gtk.EventBox()
        ev.add(inner)
        ev.connect("button-press-event", self.on_label_click)
        self.tab_box.pack_start(ev, True, True, 0)
        self.tab_box.pack_start(close, False, False, 0)
        self.tab_box.show_all()
        for w in (self.usage_label, self.count_label):
            w.set_no_show_all(True)
            w.hide()

    def draw_spring(self, w, cr):
        """La pestaña a la que se apunta durante un arrastre: naranja, y se llena hasta abrirse."""
        if self.spring_frac is None:
            return False
        import math
        a = w.get_allocation()
        x, y, wd, ht, r = -6, -4, a.width + 12, a.height + 8, 7

        def rounded(width):
            cr.new_sub_path()
            cr.arc(x + width - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + width - r, y + ht - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + ht - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()
        cr.set_source_rgba(1.0, 0.62, 0.35, 0.16)
        rounded(wd)
        cr.fill()
        if self.spring_frac > 0:
            cr.save()
            cr.rectangle(x, y, wd * self.spring_frac, ht)
            cr.clip()
            cr.set_source_rgba(1.0, 0.62, 0.35, 0.38)
            rounded(wd)
            cr.fill()
            cr.restore()
        cr.set_source_rgba(1.0, 0.62, 0.35, 0.95)
        cr.set_line_width(2)
        rounded(wd)
        cr.stroke()
        return False

    def set_hint(self, text):
        if text:
            self.hint.set_text(text)
        self.hint.set_opacity(1 if text else 0)

    # -- árbol de paneles
    def build(self, node, saved_at):
        if "split" in node and len(node.get("children") or []) == 2:
            paned = self.new_paned(node["split"], node.get("ratio", 0.5))
            paned.pack1(self.build(node["children"][0], saved_at), True, False)
            paned.pack2(self.build(node["children"][1], saved_at), True, False)
            return paned
        data = dict(node.get("pane") or {}, _saved_at=saved_at)
        pane = Pane(self, data)
        self.panes.append(pane)
        return pane.widget

    def new_paned(self, split, ratio):
        orient = Gtk.Orientation.HORIZONTAL if split == "h" else Gtk.Orientation.VERTICAL
        paned = Gtk.Paned(orientation=orient)
        PANED_RATIOS[paned] = ratio
        state = {"placed": False}

        def on_alloc(w, alloc):
            size = alloc.width if orient == Gtk.Orientation.HORIZONTAL else alloc.height
            if size > 20 and not state["placed"]:
                state["placed"] = True
                w.set_position(int(size * PANED_RATIOS.get(w, 0.5)))

        def on_pos(w, _p):
            if not state["placed"]:
                return
            a = w.get_allocation()
            size = a.width if orient == Gtk.Orientation.HORIZONTAL else a.height
            if size > 20:
                PANED_RATIOS[w] = round(w.get_position() / size, 3)
                self.app.request_save()
        paned.connect("size-allocate", on_alloc)
        paned.connect("notify::position", on_pos)
        return paned

    def pane_for_widget(self, widget):
        return next((p for p in self.panes if p.widget is widget), None)

    def node(self, widget):
        pane = self.pane_for_widget(widget)
        if pane:
            return {"pane": pane.serialize()}
        split = "h" if widget.get_orientation() == Gtk.Orientation.HORIZONTAL else "v"
        return {"split": split, "ratio": PANED_RATIOS.get(widget, 0.5),
                "children": [self.node(widget.get_child1()), self.node(widget.get_child2())]}

    def root(self):
        kids = self.box.get_children()
        return kids[0] if kids else None

    def leaf_order(self):
        out = []

        def walk(w):
            pane = self.pane_for_widget(w)
            if pane:
                out.append(pane)
            elif isinstance(w, Gtk.Paned):
                walk(w.get_child1())
                walk(w.get_child2())
        if self.root() is not None:
            walk(self.root())
        return out

    @staticmethod
    def replace_child(parent, old, new):
        if isinstance(parent, Gtk.Paned):
            first = parent.get_child1() is old
            parent.remove(old)
            (parent.pack1 if first else parent.pack2)(new, True, False)
        else:
            parent.remove(old)
            parent.pack_start(new, True, True, 0)

    def split(self, pane, split):
        """Divide el panel en dos: 'h' = lado a lado, 'v' = uno encima del otro."""
        self.unzoom()
        new = Pane(self, {"cwd": pane.cwd})
        parent = pane.widget.get_parent()
        paned = self.new_paned(split, 0.5)
        self.replace_child(parent, pane.widget, paned)
        paned.pack1(pane.widget, True, False)
        paned.pack2(new.widget, True, False)
        paned.show_all()
        self.panes.insert(self.panes.index(pane) + 1, new)
        GLib.idle_add(lambda: new.start() and False)
        GLib.idle_add(lambda: new.term.grab_focus() and False)
        self.focused = new
        self.win.refresh_titles()
        self.app.request_save()

    def remove_pane(self, pane, user=False):
        if pane not in self.panes:
            return
        self.unzoom()
        if pane.live_claude:
            self.app.claude_ended(pane.live_claude)
        pane.archive()
        if len(self.panes) == 1:
            self.panes.remove(pane)
            self.win.remove_tab(self, user)
            return
        self.unlink(pane)
        if self.focused is pane or self.focused not in self.panes:
            self.focused = self.leaf_order()[0]
        GLib.idle_add(lambda: self.focused and self.focused.term.grab_focus() and False)
        self.win.refresh_titles()
        self.app.request_save()

    def unlink(self, pane):
        """Saca el panel del árbol (su hermano ocupa el lugar). No lo cierra."""
        self.panes.remove(pane)
        paned = pane.widget.get_parent()
        sibling = paned.get_child2() if paned.get_child1() is pane.widget else paned.get_child1()
        grand = paned.get_parent()
        paned.remove(pane.widget)
        paned.remove(sibling)
        self.replace_child(grand, paned, sibling)
        PANED_RATIOS.pop(paned, None)

    def detach_pane(self, pane, win=None, pos=None):
        """Mueve el panel a una pestaña nueva (en `win`, en la posición `pos`), con su shell viva."""
        if len(self.panes) < 2 or pane not in self.panes:
            return
        win = win or self.win
        self.unzoom()
        self.unlink(pane)
        if self.focused is pane:
            self.focused = self.leaf_order()[0]
        tab = Tab(win, adopt=pane)
        win.nb.set_current_page(win.add_tab(tab, after_current=pos is None, start=False, pos=pos))
        win.present()
        GLib.idle_add(lambda: pane.term.grab_focus() and False)
        for w in {self.win, win}:
            w.refresh_titles()
        self.app.request_save()

    def dock(self, src, target, side):
        """Mete todos los paneles de la pestaña `src` junto al panel `target`."""
        if src is self or target not in self.panes or not src.panes:
            return
        src.unzoom()
        self.unzoom()
        src_root = src.root()
        src.box.remove(src_root)
        moved, focus = list(src.panes), src.current_pane()
        for p in moved:
            p.tab = self
        src.panes = []
        parent = target.widget.get_parent()
        paned = self.new_paned("h" if side in ("left", "right") else "v", 0.5)
        self.replace_child(parent, target.widget, paned)
        first, second = (src_root, target.widget) if side in ("left", "top") else (target.widget, src_root)
        paned.pack1(first, True, False)
        paned.pack2(second, True, False)
        paned.show_all()
        self.panes += moved
        if not self.custom_title and src.custom_title:
            self.custom_title = src.custom_title
        src.win.remove_tab(src)
        self.win.nb.set_current_page(self.win.nb.page_num(self.widget))
        self.win.present()
        self.focused = focus
        GLib.idle_add(lambda: focus and focus.term.grab_focus() and False)
        self.win.refresh_titles()
        self.app.request_save()

    # -- soltar una pestaña arrastrada sobre esta
    def drop_target(self, x, y, exclude=None):
        best = None
        for p in self.panes:
            if p is exclude or not p.widget.get_mapped():
                continue
            x1, y1, x2, y2 = self.rect(p)
            if x1 <= x <= x2 and y1 <= y <= y2:
                w, h = max(x2 - x1, 1), max(y2 - y1, 1)
                d = {"left": (x - x1) / w, "right": (x2 - x) / w, "top": (y - y1) / h, "bottom": (y2 - y) / h}
                best = (p, min(d, key=d.get))
        return best

    def set_drop(self, drop):
        if drop != self.drop:
            self.drop = drop
            self.dropzone.queue_draw()

    def draw_drop(self, _w, cr):
        if not self.drop:
            return False
        pane, side = self.drop
        x1, y1, x2, y2 = self.rect(pane)
        w, h = x2 - x1, y2 - y1
        x, y = x1, y1
        if side == "left":
            w /= 2
        elif side == "right":
            x, w = x1 + w / 2, w / 2
        elif side == "top":
            h /= 2
        else:
            y, h = y1 + h / 2, h / 2
        cr.set_source_rgba(1.0, 0.62, 0.35, 0.22)
        cr.rectangle(x + 3, y + 3, w - 6, h - 6)
        cr.fill_preserve()
        cr.set_source_rgba(1.0, 0.62, 0.35, 0.9)
        cr.set_line_width(2)
        cr.stroke()
        return False

    def on_drag_motion(self, widget, ctx, x, y, time_):
        src, pane = self.app.dragging, self.app.dragging_pane
        if (pane is None and (src is None or src is self)) or widget.drag_dest_find_target(ctx, None) is None:
            self.set_drop(None)
            Gdk.drag_status(ctx, 0, time_)
            return False
        self.set_drop(self.drop_target(x, y, exclude=pane))
        Gdk.drag_status(ctx, Gdk.DragAction.MOVE if self.drop else 0, time_)
        return True

    def on_drag_drop(self, _w, ctx, x, y, time_):
        src, pane = self.app.dragging, self.app.dragging_pane
        drop = self.drop_target(x, y, exclude=pane)
        self.set_drop(None)
        ok = drop is not None and (pane is not None or (src is not None and src is not self))
        Gtk.drag_finish(ctx, ok, False, time_)
        if ok and pane is not None:
            GLib.timeout_add(100, lambda: self.move_pane_here(pane, drop[0], drop[1]) and False)
        elif ok:
            # El cuaderno todavía está terminando el arrastre: se mueve después.
            self.app.pending_dock = (self, src, drop[0], drop[1])
            GLib.timeout_add(150, self.app.run_pending_dock)
        return True

    def move_pane_here(self, pane, target, side):
        """Saca `pane` de su pestaña (de esta o de otra) y lo pone junto a `target`."""
        src = pane.tab
        if pane is target or target not in self.panes or pane not in src.panes or len(src.panes) < 2:
            return
        src.unzoom()
        self.unzoom()
        src.unlink(pane)
        if src.focused is pane:
            src.focused = src.leaf_order()[0]
        pane.tab = self
        parent = target.widget.get_parent()
        paned = self.new_paned("h" if side in ("left", "right") else "v", 0.5)
        self.replace_child(parent, target.widget, paned)
        first, second = (pane.widget, target.widget) if side in ("left", "top") else (target.widget, pane.widget)
        paned.pack1(first, True, False)
        paned.pack2(second, True, False)
        paned.show_all()
        self.panes.append(pane)
        self.focused = pane
        self.win.nb.set_current_page(self.win.nb.page_num(self.widget))
        self.win.present()
        GLib.idle_add(lambda: pane.term.grab_focus() and False)
        for w in {src.win, self.win}:
            w.refresh_titles()
        self.app.request_save()

    # -- foco y navegación
    def current_pane(self):
        return self.focused if self.focused in self.panes else (self.panes[0] if self.panes else None)

    def set_focused(self, pane):
        if self.focused is not pane:
            self.focused = pane
            self.win.refresh_titles()
            self.app.request_save()

    def rect(self, pane):
        pos = pane.widget.translate_coordinates(self.widget, 0, 0)
        a = pane.widget.get_allocation()
        x, y = pos if pos else (0, 0)
        return x, y, x + a.width, y + a.height

    def focus_dir(self, direction):
        cur = self.current_pane()
        if not cur or len(self.panes) < 2:
            return
        cx1, cy1, cx2, cy2 = self.rect(cur)
        ccx, ccy = (cx1 + cx2) / 2, (cy1 + cy2) / 2
        best, best_score = None, None
        for p in self.panes:
            if p is cur or not p.widget.get_visible():
                continue
            x1, y1, x2, y2 = self.rect(p)
            if direction == "left" and x2 <= cx1 + 4:
                gap, off = cx1 - x2, abs((y1 + y2) / 2 - ccy)
            elif direction == "right" and x1 >= cx2 - 4:
                gap, off = x1 - cx2, abs((y1 + y2) / 2 - ccy)
            elif direction == "up" and y2 <= cy1 + 4:
                gap, off = cy1 - y2, abs((x1 + x2) / 2 - ccx)
            elif direction == "down" and y1 >= cy2 - 4:
                gap, off = y1 - cy2, abs((x1 + x2) / 2 - ccx)
            else:
                continue
            score = gap * 2 + off
            if best_score is None or score < best_score:
                best, best_score = p, score
        if best:
            best.term.grab_focus()

    def toggle_zoom(self):
        """Maximiza el panel actual dentro de la pestaña (y lo devuelve)."""
        if self.zoomed:
            self.unzoom()
            return
        cur = self.current_pane()
        if not cur or len(self.panes) < 2:
            return
        w = cur.widget
        while w is not self.root():
            parent = w.get_parent()
            sib = parent.get_child2() if parent.get_child1() is w else parent.get_child1()
            sib.hide()
            self.hidden.append(sib)
            w = parent
        self.zoomed = cur
        cur.term.grab_focus()
        self.win.refresh_titles()

    def unzoom(self):
        for w in self.hidden:
            w.show()
        self.hidden = []
        if self.zoomed:
            self.zoomed = None
            self.win.refresh_titles()

    # -- datos
    def title(self):
        if self.custom_title:
            return self.custom_title
        cur = self.current_pane()
        return cur.title() if cur else "…"

    def serialize(self):
        leaves = self.leaf_order()
        cur = self.current_pane()
        return {"tab_id": self.id, "title": self.custom_title,
                "focused": leaves.index(cur) if cur in leaves else 0,
                "layout": self.node(self.root())}

    def start(self):
        for p in self.panes:
            p.start()

    def foreground_running(self):
        return sum(1 for p in self.panes if p.foreground_running())

    def request_close(self):
        running = self.foreground_running()
        if running and not self.win.confirm(
                T_count(running, "Hay un proceso corriendo en esta pestaña.",
                        "Hay {n} procesos corriendo en esta pestaña."), T("Cerrar pestaña")):
            return
        for p in list(self.panes):
            if p.live_claude:
                self.app.claude_ended(p.live_claude)
            p.archive()
            p.kill()
        self.win.remove_tab(self, user=True)

    def kill(self):
        for p in self.panes:
            p.kill()

    def on_label_click(self, _w, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self.win.rename_tab(self)
            return True
        if event.button == 2:
            self.request_close()
            return True
        return False


# ---------------------------------------------------------------- ventana

class TermWindow(Gtk.ApplicationWindow):
    def __init__(self, app, saved=None, empty=False):
        super().__init__(application=app, title="Hilo")
        self.app = app
        self.tabs = []
        self.closing = False
        saved = saved or {}
        self.set_default_size(saved.get("width", 900), saved.get("height", 560))
        if saved.get("maximized"):
            self.maximize()
        self.set_icon_name(APP_ID)

        self.active_tab = self.prev_tab = None
        self.nb = Gtk.Notebook(scrollable=True, show_border=False)
        self.nb.get_style_context().add_class("hilo-tabs")
        # Mismo grupo en todas las ventanas: las pestañas se pueden pasar de una a otra.
        self.nb.set_group_name("hilo")
        self.nb.connect("drag-begin", self.on_tab_drag_begin)
        self.nb.connect_after("drag-end", self.on_tab_drag_end)
        self.nb.connect("page-added", self.on_page_added)
        self.nb.connect("page-removed", self.on_page_removed)
        self.nb.connect("create-window", self.on_create_window)
        self.nb.connect("switch-page", self.on_switch_page)
        self.nb.connect("page-reordered", lambda *_: self.app.request_save())
        # Al arrastrar, quedarse un momento sobre otra pestaña la abre, para soltar en sus paneles.
        self.spring = None          # (pestaña, id del temporizador)
        self.nb.connect("drag-motion", self.on_nb_drag_motion)
        self.nb.connect("drag-leave", lambda *_: self.cancel_spring())
        self.nb.connect("drag-drop", self.on_nb_drag_drop)
        self.nb.connect_after("draw", self.on_nb_draw)
        self.nb.drag_dest_get_target_list().add(Gdk.Atom.intern("HILO_PANE", False), Gtk.TargetFlags.SAME_APP, 1)
        # Botones al final de la barra: nueva pestaña y ocultar la barra.
        self.plus_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU)
        self.plus_btn.connect("clicked", lambda *_: self.new_tab())
        self.prefs_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU)
        self.prefs_btn.connect("clicked", lambda *_: self.app.show_preferences())
        self.help_btn = Gtk.Button.new_from_icon_name("input-keyboard-symbolic", Gtk.IconSize.MENU)
        self.help_btn.connect("clicked", lambda *_: self.app.show_help())
        self.hide_btn = Gtk.Button.new_from_icon_name("view-conceal-symbolic", Gtk.IconSize.MENU)
        self.hide_btn.connect("clicked", lambda *_: self.app.toggle_tabs())
        bar_btns = Gtk.Box(spacing=0)
        self.update_btn = Gtk.Button(label=T("Actualizar"))
        self.update_btn.get_style_context().add_class("hilo-update")
        self.update_btn.set_focus_on_click(False)
        self.update_btn.set_valign(Gtk.Align.CENTER)
        self.update_btn.set_margin_end(8)
        self.update_btn.set_tooltip_text(T("Hay una versión nueva de Hilo. Se aplica sin cerrar nada: "
                                           "las shells, Claude y lo que esté corriendo siguen vivos."))
        self.update_btn.connect("clicked", lambda *_: self.app.hot_reload())
        self.update_btn.set_no_show_all(True)
        bar_btns.pack_start(self.update_btn, False, False, 0)
        # Consumo de todo el equipo (CPU, temperatura y RAM).
        self.sys_label = Gtk.Label(margin_end=10)
        self.sys_label.get_style_context().add_class("hilo-sys")
        self.sys_label.set_tooltip_text(T("Todo el equipo: uso de CPU, temperatura del procesador y RAM usada / total"))
        bar_btns.pack_start(self.sys_label, False, False, 0)
        # Lectura en voz alta de las respuestas de Claude (solo si hay piper y una voz).
        self.tts_btn = Gtk.Button.new_from_icon_name("audio-volume-medium-symbolic", Gtk.IconSize.MENU)
        self.tts_btn.get_style_context().add_class("hilo-tts")
        self.tts_btn.connect("clicked", lambda *_: self.toggle_tts_panel())
        self.tts_btn.set_no_show_all(True)
        self.tts_panel = None
        for b in (self.tts_btn, self.plus_btn, self.prefs_btn, self.help_btn, self.hide_btn):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_focus_on_click(False)
            bar_btns.pack_start(b, False, False, 0)
        bar_btns.show_all()
        self.update_system()
        self.nb.set_action_widget(bar_btns, Gtk.PackType.END)
        self.update_update_btn()
        self.update_bar_tooltips()
        self.update_tts()

        self.search = Gtk.SearchEntry(placeholder_text=T("Buscar en el panel (Enter: anterior, Shift+Enter: siguiente)"))
        self.search.connect("search-changed", self.on_search_changed)
        self.search.connect("activate", lambda *_: self.search_step(back=True))
        self.search.connect("key-press-event", self.on_search_key)
        self.search_bar = Gtk.SearchBar()
        self.search_bar.add(self.search)
        self.search_bar.connect_entry(self.search)
        self.search.set_hexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(self.nb, True, True, 0)
        box.pack_start(self.search_bar, False, False, 0)
        self.add(box)
        self.connect("key-press-event", self.on_key)
        self.connect("delete-event", self.on_delete)
        self.connect("window-state-event", lambda *_: self.app.request_save())
        self.connect("configure-event", lambda *_: self.app.request_save() or False)
        box.show_all()

        for t in saved.get("tabs", []):
            t = dict(t, _saved_at=self.app.saved_at)
            self.add_tab(Tab(self, t))
        if not self.tabs and not empty:
            self.add_tab(Tab(self))
        active = saved.get("active", 0)
        if 0 <= active < len(self.tabs):
            self.nb.set_current_page(active)
        self.update_tabs_visibility()

    # -- pestañas
    def current(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        return next((t for t in self.tabs if t.widget is page), None)

    def current_pane(self):
        tab = self.current()
        return tab.current_pane() if tab else None

    def panes(self):
        return [p for t in self.tabs for p in t.panes]

    def ordered_tabs(self):
        by_widget = {t.widget: t for t in self.tabs}
        return [by_widget[self.nb.get_nth_page(i)] for i in range(self.nb.get_n_pages())
                if self.nb.get_nth_page(i) in by_widget]

    def add_tab(self, tab, after_current=False, start=True, pos=None):
        self.tabs.append(tab)
        if pos is None:
            pos = self.nb.get_current_page() + 1 if after_current else -1
        idx = self.nb.insert_page(tab.widget, tab.tab_box, pos)
        self.nb.set_tab_reorderable(tab.widget, True)
        self.nb.child_set_property(tab.widget, "tab-expand", True)
        self.nb.set_tab_detachable(tab.widget, True)
        self.update_tabs_visibility()
        if start:
            GLib.idle_add(lambda: tab.start() and False)
        return idx

    # -- arrastrar una pestaña para convertirla en panel
    def on_tab_drag_begin(self, *_):
        self.app.dragging = self.current()
        self.app.show_drag_hints()
        # Mientras se arrastra, todas las ventanas muestran su barra para poder soltar ahí.
        for w in self.app.windows:
            w.nb.set_show_tabs(True)
        prev = self.prev_tab
        # Al tomarla, GTK la muestra; se vuelve a la anterior para soltarla encima.
        if prev in self.tabs and prev is not self.app.dragging:
            GLib.idle_add(lambda: self.nb.set_current_page(self.nb.page_num(prev.widget)) and False)

    def on_nb_draw(self, nb, cr):
        """Mientras se arrastra una pestaña, GTK no dibuja la pestaña abierta (cree que es la
        que va en la mano). Como Hilo abre otra para poder soltar ahí, se dibuja aquí."""
        drag = self.app.dragging
        cur = self.current()
        if drag is None or cur is None or cur is drag or not cur.tab_box.get_mapped():
            return False
        pos = cur.tab_box.translate_coordinates(nb, 0, 0)
        if not pos:
            return False
        a = cur.tab_box.get_allocation()
        x, y = pos
        cr.save()
        ctx = nb.get_style_context()
        ok, bg = ctx.lookup_color("theme_base_color")
        if ok:
            cr.set_source_rgba(bg.red, bg.green, bg.blue, 1)
            cr.rectangle(x - 8, y - 6, a.width + 16, a.height + 12)
            cr.fill()
        cr.set_source_rgba(1.0, 0.62, 0.35, 1)       # la línea naranja de la pestaña abierta
        cr.rectangle(x - 8, y + a.height + 3, a.width + 16, 3)
        cr.fill()
        cr.translate(x, y)
        cur.draw_spring(cur.tab_box, cr)
        cr.restore()
        cr.save()
        nb.propagate_draw(cur.tab_box, cr)
        cr.restore()
        return False

    def tab_at(self, x, y):
        """La pestaña cuya etiqueta está en (x, y), en coordenadas del cuaderno."""
        for t in self.tabs:
            if not t.tab_box.get_mapped():
                continue
            pos = t.tab_box.translate_coordinates(self.nb, 0, 0)
            a = t.tab_box.get_allocation()
            if pos and pos[0] - 6 <= x <= pos[0] + a.width + 6 and pos[1] - 6 <= y <= pos[1] + a.height + 6:
                return t
        return None

    def on_nb_drag_motion(self, _nb, _ctx, x, y, _time):
        src = self.app.dragging or self.app.dragging_pane
        hover = self.tab_at(x, y) if src is not None else None
        if hover is None or hover is src or hover is self.current():
            self.cancel_spring()
        elif not self.spring or self.spring[0] is not hover:
            self.cancel_spring()
            hover.spring_frac = 0.0
            hover.tab_box.queue_draw()
            self.spring = (hover, GLib.timeout_add(25, self.spring_tick, hover, time.monotonic()))
        if self.app.dragging_pane is not None:
            Gdk.drag_status(_ctx, Gdk.DragAction.MOVE if self.in_tab_bar(y) else 0, _time)
            return True
        return False    # GTK sigue con lo suyo (reordenar al soltar sobre la barra)

    def in_tab_bar(self, y):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        pos = page.translate_coordinates(self.nb, 0, 0) if page else None
        return pos is not None and y < pos[1]

    def on_nb_drag_drop(self, _nb, ctx, x, y, time_):
        pane = self.app.dragging_pane
        if pane is None:
            return False
        ok = self.in_tab_bar(y)
        Gtk.drag_finish(ctx, ok, False, time_)
        if ok:
            # Queda en la posición donde se soltó, entre las pestañas.
            pos = 0
            for i, t in enumerate(self.ordered_tabs()):
                tp = t.tab_box.translate_coordinates(self.nb, 0, 0)
                if tp and x > tp[0] + t.tab_box.get_allocated_width() / 2:
                    pos = i + 1
            GLib.timeout_add(100, lambda: pane.tab.detach_pane(pane, self, pos) and False)
        return True

    SPRING_S = 0.55     # lo que hay que esperar encima de una pestaña para que se abra

    def spring_tick(self, tab, started):
        frac = (time.monotonic() - started) / self.SPRING_S
        if frac < 1 and tab in self.tabs:
            tab.spring_frac = frac
            tab.tab_box.queue_draw()
            return True
        self.spring = None
        tab.spring_frac = None
        tab.tab_box.queue_draw()
        if tab in self.tabs:
            self.nb.set_current_page(self.nb.page_num(tab.widget))
            tab.set_hint(self.app.drag_hint())
        return False

    def cancel_spring(self):
        if self.spring:
            GLib.source_remove(self.spring[1])
            tab = self.spring[0]
            tab.spring_frac = None
            tab.tab_box.queue_draw()
            self.spring = None

    def on_tab_drag_end(self, *_):
        GLib.idle_add(self.clear_drag)

    def clear_drag(self):
        if not self.app.pending_dock:
            self.app.dragging = None
        for w in self.app.windows:
            w.cancel_spring()
            for t in w.tabs:
                t.set_hint(None)
            w.update_tabs_visibility()
            for t in w.tabs:
                t.set_drop(None)
        return False

    # -- pestañas que llegan o se van a otra ventana (lo mueve GTK al soltar)
    def on_page_removed(self, _nb, child, _n):
        tab = next((t for t in self.tabs if t.widget is child), None)
        if tab is None:
            return  # la quitamos nosotros (remove_tab)
        self.tabs.remove(tab)
        self.app.in_transit[child] = tab
        self.update_tabs_visibility()
        if not self.tabs:
            # Se fue la última pestaña: la ventana se cierra cuando termine el arrastre.
            GLib.timeout_add(300, self.close_if_empty)

    def close_if_empty(self):
        if not self.tabs and self in self.app.windows:
            self.app.window_emptied(self, True)
        return False

    def on_page_added(self, _nb, child, n):
        if any(t.widget is child for t in self.tabs):
            return  # la agregamos nosotros (add_tab)
        tab = self.app.in_transit.pop(child, None)
        if tab is None:
            return
        tab.win = self
        self.tabs.append(tab)
        self.nb.set_tab_reorderable(child, True)
        self.nb.child_set_property(child, "tab-expand", True)
        self.nb.set_tab_detachable(child, True)
        self.update_tabs_visibility()
        self.nb.set_current_page(n)
        cur = tab.current_pane()
        if cur:
            GLib.idle_add(lambda: cur.term.grab_focus() and False)
        self.app.request_save()

    def on_create_window(self, _nb, page, x, y):
        """Pestaña soltada fuera de toda ventana: se abre en una ventana nueva."""
        w, h = self.get_size()
        win = self.app.spawn_window({"width": w, "height": h}, empty=True)
        win.move(max(x - 40, 0), max(y - 20, 0))
        return win.nb

    def open_tab(self, saved):
        tab = Tab(self, saved)
        self.nb.set_current_page(self.add_tab(tab, after_current=True))
        GLib.idle_add(lambda: tab.current_pane() and tab.current_pane().term.grab_focus() and False)
        self.app.request_save()
        return tab

    def new_tab(self):
        cur = self.current_pane()
        self.open_tab({"cwd": cur.cwd} if cur else None)

    def remove_tab(self, tab, user=False):
        if tab not in self.tabs:
            return
        self.tabs.remove(tab)
        n = self.nb.page_num(tab.widget)
        if n >= 0:
            self.nb.remove_page(n)
        self.update_tabs_visibility()
        if not self.tabs:
            self.app.window_emptied(self, user)
        else:
            cur = self.current_pane()
            if cur:
                cur.term.grab_focus()
            self.app.request_save()

    def update_tabs_visibility(self):
        # La barra se ve siempre (tiene los botones), salvo en el modo oculto.
        # Durante un arrastre se muestra igual, para poder soltar la pestaña en ella.
        self.nb.set_show_tabs(not self.app.config["tabs_hidden"] or bool(self.app.dragging or self.app.dragging_pane))
        self.refresh_titles()

    def update_update_btn(self):
        self.update_btn.set_visible(self.app.update_ready)

    def update_tts(self, progress_only=False):
        tts = self.app.tts
        show = bool(tts and tts.owner)
        self.tts_btn.set_visible(show)
        if not show:
            return
        if not progress_only:
            p = tts.player
            st = p.state if tts.enabled or p.state != "idle" else "off"
            self.tts_btn.get_image().set_from_icon_name({
                "playing": "audio-volume-high-symbolic", "paused": "media-playback-pause-symbolic",
                "idle": "audio-volume-medium-symbolic", "off": "audio-volume-muted-symbolic"}[st],
                Gtk.IconSize.MENU)
            ctx = self.tts_btn.get_style_context()
            (ctx.add_class if p.state != "idle" else ctx.remove_class)("active")
        if self.tts_panel and self.tts_panel.get_visible():
            self.tts_panel.refresh(progress_only=progress_only)

    def toggle_tts_panel(self):
        if not (self.app.tts and self.app.tts.owner):
            return
        if self.tts_panel is None:
            self.tts_panel = TtsPanel(self)
        if self.tts_panel.get_visible():
            self.tts_panel.popdown()
            return
        if self.tts_btn.get_mapped():
            self.tts_panel.set_relative_to(self.tts_btn)
            r = Gdk.Rectangle()
            a = self.tts_btn.get_allocation()
            r.x, r.y, r.width, r.height = 0, 0, a.width, a.height
            self.tts_panel.set_pointing_to(r)
        else:
            # Con la barra oculta, el panel sale arriba a la derecha del panel activo.
            pane = self.current_pane()
            if not pane:
                return
            self.tts_panel.set_relative_to(pane.term)
            r = Gdk.Rectangle()
            r.x, r.y, r.width, r.height = pane.term.get_allocated_width() - 40, 0, 1, 1
            self.tts_panel.set_pointing_to(r)
        self.tts_panel.popup()

    def update_system(self):
        text = self.app.system_text if self.app.config["show_system"] else None
        if not text:
            self.sys_label.hide()
            return
        if self.sys_label.get_text() != text:
            self.sys_label.set_text(text)
        ctx = self.sys_label.get_style_context()
        (ctx.add_class if self.app.system_hot else ctx.remove_class)("hot")
        self.sys_label.show()

    def update_bar_tooltips(self):
        def tip(text, action):
            hint = self.accel_hint(action)
            return f"{text} ({hint})" if hint else text
        self.plus_btn.set_tooltip_text(tip(T("Nueva pestaña"), "new_tab"))
        self.prefs_btn.set_tooltip_text(tip(T("Preferencias"), "preferences"))
        self.help_btn.set_tooltip_text(tip(T("Atajos y controles"), "show_help"))
        self.hide_btn.set_tooltip_text(tip(T("Ocultar las pestañas"), "toggle_tabs"))
        self.tts_btn.set_tooltip_text(tip(T("Lectura en voz alta"), "tts_panel"))

    def refresh_titles(self):
        tts = self.app.tts
        speaking = tts.pane_for(tts.speaking()) if tts and tts.owner else None
        for t in self.tabs:
            multi = len(t.panes) > 1 and not t.zoomed
            cur = t.current_pane()
            for p in t.panes:
                ctx = p.widget.get_style_context()
                (ctx.add_class if multi else ctx.remove_class)("multi")
                (ctx.add_class if p is cur else ctx.remove_class)("focused")
            title = t.title()
            n = len(t.panes)
            shown = ("♪ " if speaking in t.panes else "") + \
                (f"{'⤢ ' if t.zoomed else ''}▦ {n}   " if n > 1 else "") + title
            if t.label.get_text() != shown:
                t.label.set_text(shown)
            tip = title + ("\n" + T("{n} paneles", n=n) if n > 1 else "")
            if t.usage:
                ram, cpu, detail = t.usage
                text = f"{fmt_mem(ram)} · {fmt_cpu(cpu)}"
                if t.usage_label.get_text() != text:
                    t.usage_label.set_text(text)
                tip = f"{tip}\n\n{detail}"
            else:
                t.usage_label.set_text("")
            # Visible u oculto en todas por igual, para que midan lo mismo.
            t.usage_label.set_visible(bool(self.app.config["show_usage"]))
            if t.tab_box.get_tooltip_text() != tip:
                t.tab_box.set_tooltip_text(tip)
        cur = self.current()
        if cur:
            title = cur.title()
            # Sin barra visible, el título dice en qué pestaña va y cuánto consume.
            if self.app.config["tabs_hidden"]:
                if len(self.tabs) > 1:
                    title = f"[{self.nb.page_num(cur.widget) + 1}/{len(self.tabs)}]  {title}"
                if len(cur.panes) > 1:
                    title += "  ·  " + T("{n} paneles", n=len(cur.panes))
                if cur.usage:
                    title += f"  —  {fmt_mem(cur.usage[0])} · {fmt_cpu(cur.usage[1])}"
            self.set_title(title)

    def on_switch_page(self, _nb, page, _n):
        tab = next((t for t in self.tabs if t.widget is page), None)
        if tab is not self.active_tab:
            self.prev_tab, self.active_tab = self.active_tab, tab
        if tab:
            GLib.idle_add(lambda: self.refresh_titles() and False)
            GLib.idle_add(lambda: tab.current_pane() and tab.current_pane().term.grab_focus() and False)
        self.app.request_save()

    def serialize(self):
        w, h = self.get_size()
        maxed = self.is_maximized()
        if maxed and self.app.last_sizes.get(id(self)):
            w, h = self.app.last_sizes[id(self)]
        elif not maxed:
            self.app.last_sizes[id(self)] = (w, h)
        return {"width": w, "height": h, "maximized": maxed,
                "active": max(self.nb.get_current_page(), 0),
                "tabs": [t.serialize() for t in self.ordered_tabs()]}

    # -- diálogos
    def confirm(self, text, action):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text=text)
        d.add_buttons(T("Cancelar"), Gtk.ResponseType.CANCEL, action, Gtk.ResponseType.ACCEPT)
        d.set_default_response(Gtk.ResponseType.CANCEL)
        ok = d.run() == Gtk.ResponseType.ACCEPT
        d.destroy()
        return ok

    def rename_tab(self, tab):
        d = Gtk.Dialog(title=T("Renombrar pestaña"), transient_for=self, modal=True)
        d.add_buttons(T("Cancelar"), Gtk.ResponseType.CANCEL, T("Guardar"), Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        e = Gtk.Entry(text=tab.custom_title or "", activates_default=True,
                      placeholder_text=T("Vacío = nombre automático"))
        e.set_margin_start(12); e.set_margin_end(12); e.set_margin_top(12); e.set_margin_bottom(12)
        d.get_content_area().add(e)
        d.show_all()
        if d.run() == Gtk.ResponseType.OK:
            tab.custom_title = e.get_text().strip() or None
            self.refresh_titles()
            self.app.request_save()
        d.destroy()

    def accel_hint(self, action):
        accels = self.app.config["shortcuts"].get(action) or []
        return accel_label(accels[0]) if accels else None

    def show_context_menu(self, pane, event):
        tab = pane.tab
        m = Gtk.Menu()
        multi = len(tab.panes) > 1
        items = [
            (T("Copiar"), "copy", lambda: pane.term.copy_clipboard_format(Vte.Format.TEXT), pane.term.get_has_selection()),
            (T("Pegar"), "paste", lambda: pane.term.paste_clipboard(), True),
            None,
            (T("Dividir a la derecha"), "split_right", lambda: tab.split(pane, "h"), True),
            (T("Dividir hacia abajo"), "split_down", lambda: tab.split(pane, "v"), True),
            (T("Restaurar paneles") if tab.zoomed else T("Maximizar panel"), "zoom_pane", tab.toggle_zoom, multi),
            (T("Mover panel a una pestaña nueva"), "detach_pane", lambda: tab.detach_pane(pane), multi),
            (T("Cerrar panel"), "close_pane", pane.request_close, multi),
            None,
            (T("Nueva pestaña"), "new_tab", self.new_tab, True),
            (T("Nueva ventana"), "new_window", self.app.new_window, True),
            (T("Renombrar pestaña…"), "rename_tab", lambda: self.rename_tab(tab), True),
            (T("Cerrar pestaña"), "close_tab", tab.request_close, True),
            (T("Mostrar las pestañas") if self.app.config["tabs_hidden"] else T("Ocultar las pestañas"),
             "toggle_tabs", self.app.toggle_tabs, True),
            None,
            (T("Preferencias…"), "preferences", self.app.show_preferences, True),
        ] + ([(T("Aplicar actualización de Hilo"), "apply_update", self.app.hot_reload, True)]
             if self.app.update_ready else []) + [
        ]
        hist = self.app.recent_claude(15)
        closed = list_closed()[:15]
        for it in items:
            if it is None:
                m.append(Gtk.SeparatorMenuItem())
                continue
            label, action, fn, sensitive = it
            mi = Gtk.MenuItem()
            row = Gtk.Box(spacing=24)
            row.pack_start(Gtk.Label(label=label, xalign=0), True, True, 0)
            hint = self.accel_hint(action)
            if hint:
                hl = Gtk.Label(label=hint, xalign=1)
                hl.get_style_context().add_class("dim-label")
                row.pack_end(hl, False, False, 0)
            mi.add(row)
            mi.set_sensitive(sensitive)
            mi.connect("activate", lambda _w, f=fn: f())
            m.append(mi)
        m.append(Gtk.SeparatorMenuItem())
        for title, entries, fn, empty in (
                (T("Sesiones de Claude"), hist, self.resume_claude, T("Sin sesiones registradas")),
                (T("Paneles cerrados"), closed, self.reopen_closed, T("No hay paneles cerrados"))):
            top = Gtk.MenuItem(label=title)
            sub = Gtk.Menu()
            for e in entries:
                if "closed_at" in e:
                    when = time.strftime("%d %b %H:%M", time.localtime(e["closed_at"]))
                    label = f"{when} · {e.get('title') or pretty_dir(e.get('cwd'))}"
                    if e.get("claude_ids"):
                        label += "  (Claude)"
                else:
                    label = history_label(e)
                mi = Gtk.MenuItem(label=label)
                mi.set_tooltip_text(e.get("prompt") or e.get("last_line") or "")
                mi.connect("activate", lambda _w, x=e, f=fn: f(x))
                sub.append(mi)
            if not entries:
                mi = Gtk.MenuItem(label=empty)
                mi.set_sensitive(False)
                sub.append(mi)
            top.set_submenu(sub)
            m.append(top)
        m.show_all()
        m.attach_to_widget(self, None)
        m.popup_at_pointer(event)

    def resume_claude(self, entry):
        """Abre una pestaña nueva en la carpeta de la sesión y la reanuda."""
        argv = claude_resume_argv(entry.get("argv") or ["claude"], entry["id"])
        tab = self.open_tab({"cwd": entry.get("cwd") or HOME,
                             "command": {"argv": argv, "claude_session": entry["id"]}})
        tab.panes[0].claude_ids.append(entry["id"])

    def reopen_closed(self, meta):
        tab = Tab(self, {"cwd": meta.get("cwd") or HOME, "claude_ids": meta.get("claude_ids"),
                         "title": meta.get("title")})
        pane = tab.panes[0]
        pane.restore_scroll = meta["_txt"]
        pane.saved_at = meta.get("closed_at")
        pane.consume_scroll = True
        self.nb.set_current_page(self.add_tab(tab, after_current=True))
        self.app.request_save()

    # -- búsqueda (en el panel con foco)
    def on_search_changed(self, entry):
        pane = self.current_pane()
        if not pane:
            return
        text = entry.get_text()
        if not text:
            pane.term.search_set_regex(None, 0)
            return
        import re
        try:
            rx = Vte.Regex.new_for_search(re.escape(text), -1, PCRE2_MULTILINE | PCRE2_CASELESS)
            pane.term.search_set_regex(rx, 0)
            pane.term.search_set_wrap_around(True)
            pane.term.search_find_previous()
        except GLib.Error:
            pass

    def search_step(self, back):
        pane = self.current_pane()
        if pane:
            (pane.term.search_find_previous if back else pane.term.search_find_next)()

    def on_search_key(self, _w, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.SHIFT_MASK:
            self.search_step(back=False)
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.search_bar.set_search_mode(False)
            cur = self.current_pane()
            if cur:
                cur.term.grab_focus()
            return True
        return False

    # -- teclado (atajos configurables en Preferencias)
    def run_action(self, action):
        tab = self.current()
        pane = self.current_pane()
        fn = {
            "new_tab": self.new_tab,
            "new_window": self.app.new_window,
            "split_right": lambda: pane and tab.split(pane, "h"),
            "split_down": lambda: pane and tab.split(pane, "v"),
            "close_pane": lambda: pane and pane.request_close(),
            "detach_pane": lambda: pane and tab.detach_pane(pane),
            "zoom_pane": lambda: tab and tab.toggle_zoom(),
            "focus_left": lambda: tab and tab.focus_dir("left"),
            "focus_right": lambda: tab and tab.focus_dir("right"),
            "focus_up": lambda: tab and tab.focus_dir("up"),
            "focus_down": lambda: tab and tab.focus_dir("down"),
            "close_tab": lambda: tab and tab.request_close(),
            "close_window": self.on_delete,
            # Sin selección propia no se toca el portapapeles: VTE lo dejaría vacío y se
            # perdería lo que copió el programa (Claude a pantalla completa copia solo).
            "copy": lambda: pane and pane.term.get_has_selection() and pane.term.copy_clipboard_format(Vte.Format.TEXT),
            "paste": lambda: pane and pane.term.paste_clipboard(),
            "search": self.open_search,
            "next_tab": lambda: self.cycle(1),
            "prev_tab": lambda: self.cycle(-1),
            "move_tab_right": lambda: self.move_tab(1),
            "move_tab_left": lambda: self.move_tab(-1),
            "rename_tab": lambda: tab and self.rename_tab(tab),
            "zoom_in": lambda: self.app.zoom(1.1),
            "zoom_out": lambda: self.app.zoom(1 / 1.1),
            "zoom_reset": lambda: self.app.zoom(None),
            "fullscreen": self.toggle_fullscreen,
            "toggle_tabs": self.app.toggle_tabs,
            "preferences": self.app.show_preferences,
            "show_help": self.app.show_help,
            "apply_update": self.app.hot_reload,
            "tts_panel": self.toggle_tts_panel,
            "tts_play_pause": lambda: self.app.tts and self.app.tts.player.toggle_pause(),
            "tts_stop": lambda: self.app.tts and self.app.tts.player.stop(),
        }.get(action)
        if fn:
            fn()

    def on_key(self, _w, event):
        mods = int(event.state & Gtk.accelerator_get_default_mod_mask())
        key = Gdk.keyval_to_lower(event.keyval)
        action = self.app.keymap.get((key, mods))
        if action:
            self.run_action(action)
            return True
        if mods == int(Gdk.ModifierType.MOD1_MASK) and Gdk.KEY_1 <= key <= Gdk.KEY_9:
            n = key - Gdk.KEY_1
            if n < self.nb.get_n_pages():
                self.nb.set_current_page(n)
            return True
        return False

    def toggle_fullscreen(self):
        if self.get_window() and self.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
            self.unfullscreen()
        else:
            self.fullscreen()

    def open_search(self):
        self.search_bar.set_search_mode(True)
        self.search.grab_focus()

    def cycle(self, d):
        n = self.nb.get_n_pages()
        if n:
            self.nb.set_current_page((self.nb.get_current_page() + d) % n)

    def move_tab(self, d):
        tab = self.current()
        if not tab:
            return
        n = self.nb.get_n_pages()
        self.nb.reorder_child(tab.widget, (self.nb.get_current_page() + d) % n)
        self.app.request_save()

    def on_delete(self, *_):
        """Cerrar con la X. Si es la última ventana, la sesión queda guardada."""
        running = sum(1 for p in self.panes() if p.foreground_running())
        if running and not self.confirm(
                T_count(running, "Hay un proceso corriendo en esta ventana.",
                        "Hay {n} procesos corriendo en esta ventana.") + " "
                + T("Si la cierra, se detienen; la sesión se recuerda para la próxima vez."),
                T("Cerrar ventana")):
            return True
        self.app.close_window(self)
        return True


# ---------------------------------------------------------------- preferencias

class HelpWindow(Gtk.Window):
    """Lista de atajos (los vigentes, no los de fábrica) y gestos con el mouse."""

    def __init__(self, app):
        super().__init__(title=T("Atajos y controles"), modal=False)
        self.app = app
        self.set_default_size(560, 680)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        hb = Gtk.HeaderBar(show_close_button=True, title=T("Atajos y controles"))
        edit = Gtk.Button(label=T("Cambiar atajos…"))
        edit.connect("clicked", lambda *_: (self.app.show_preferences("atajos"), self.destroy()))
        hb.pack_start(edit)
        self.set_titlebar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=18)
        shortcuts = self.app.config["shortcuts"]
        for title, actions in HELP_GROUPS:
            rows = []
            for a in actions:
                accels = shortcuts.get(a) or []
                keys = T(" o ").join(accel_label(x) for x in accels[:2]) if accels else T("sin atajo")
                rows.append((ACTION_LABELS[a], keys, not accels))
            self.section(box, title, rows)
        self.section(box, T("Teclas fijas y mouse"), [(a, b, False) for a, b in HELP_FIXED])

        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add(box)
        self.add(sc)
        self.connect("key-press-event", lambda _w, e: e.keyval == Gdk.KEY_Escape and (self.destroy() or True))
        self.show_all()

    def section(self, box, title, rows):
        head = Gtk.Label(xalign=0, margin_top=12)
        head.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        box.pack_start(head, False, False, 0)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.get_style_context().add_class("frame")
        for label, keys, dim in rows:
            row = Gtk.Box(spacing=18, margin=8, margin_start=12, margin_end=12)
            row.pack_start(Gtk.Label(label=label, xalign=0, wrap=True), True, True, 0)
            k = Gtk.Label(label=keys, xalign=1)
            k.get_style_context().add_class("dim-label")
            if not dim:
                k.set_markup(f"<b>{GLib.markup_escape_text(keys)}</b>")
            row.pack_end(k, False, False, 0)
            lb.add(row)
        box.pack_start(lb, False, False, 0)


class PreferencesWindow(Gtk.Window):
    def __init__(self, app):
        super().__init__(title=T("Preferencias de Hilo"), modal=False)
        self.app = app
        self.set_default_size(560, 620)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.capturing = None   # (acción, botón) mientras espera la tecla nueva

        stack = self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER)
        hb = Gtk.HeaderBar(show_close_button=True, custom_title=switcher)
        self.set_titlebar(hb)
        stack.add_titled(self.build_general(), "general", T("General"))
        stack.add_titled(self.build_shortcuts(), "atajos", T("Atajos"))
        self.add(stack)
        self.connect("key-press-event", self.on_key)
        self.show_all()

    # -- general
    def build_general(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, margin=18)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.get_style_context().add_class("frame")
        rows = [
            ("show_usage", T("Mostrar RAM y CPU en las pestañas"),
             T("La suma de la shell y de todo lo que corre dentro.")),
            ("show_system", T("Mostrar CPU, temperatura y RAM del equipo"),
             T("En la barra de pestañas, para todo el equipo.")),
            ("autorun_claude", T("Reanudar Claude Code solo al restaurar"),
             T("Si se apaga, el comando queda escrito esperando Enter.")),
        ]
        for key, title, sub in rows:
            sw = Gtk.Switch(active=bool(self.app.config[key]), valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p, k=key: self.set_option(k, w.get_active()))
            lb.add(self.row(title, sub, sw))
        entry = Gtk.Entry(text=", ".join(self.app.config["autorun"]), width_chars=22, valign=Gtk.Align.CENTER)
        entry.connect("changed", lambda e: self.set_option(
            "autorun", [x.strip() for x in e.get_text().split(",") if x.strip()]))
        lb.add(self.row(T("Programas que se vuelven a abrir solos"),
                        T("Separados por coma. El resto queda escrito esperando Enter."), entry))
        if self.app.tts:
            sw = Gtk.Switch(active=self.app.tts.enabled, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p: self.app.tts.set_enabled(w.get_active()))
            lb.add(self.row(T("Leer en voz alta las respuestas de Claude"),
                            T("Con piper. El historial y los controles están en el botón del parlante."), sw))
        lang = Gtk.ComboBoxText(valign=Gtk.Align.CENTER)
        for code, name in (("auto", T("Automático (el del sistema)")), ("es", "Español"), ("en", "English")):
            lang.append(code, name)
        lang.set_active_id(self.app.config.get("language", "auto"))
        lang.connect("changed", lambda c: self.set_language(c.get_active_id()))
        lb.add(self.row(T("Idioma"), T("Se aplica al momento, sin cerrar nada."), lang))
        box.pack_start(lb, False, False, 0)
        info = Gtk.Label(xalign=0, wrap=True, margin_top=14)
        info.get_style_context().add_class("dim-label")
        info.set_markup("<small>" + GLib.markup_escape_text(T("La configuración se guarda en {path}", path=pretty_dir(CONFIG_FILE))) + "</small>")
        box.pack_start(info, False, False, 0)
        return box

    def row(self, title, subtitle, widget):
        r = Gtk.ListBoxRow(activatable=False)
        h = Gtk.Box(spacing=12, margin=12)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        v.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
        if subtitle:
            sub = Gtk.Label(xalign=0, wrap=True)
            sub.set_markup(f"<small>{GLib.markup_escape_text(subtitle)}</small>")
            sub.get_style_context().add_class("dim-label")
            v.pack_start(sub, False, False, 0)
        h.pack_start(v, True, True, 0)
        h.pack_end(widget, False, False, 0)
        r.add(h)
        return r

    def set_option(self, key, value):
        self.app.config[key] = value
        self.app.save_config()
        if key == "show_system":
            if value:
                self.app.system_text, self.app.system_hot, self.app._sys_prev = system_sample(None)
            for w in self.app.windows:
                w.update_system()
        if key == "show_usage":
            for w in self.app.windows:
                for t in w.tabs:
                    t.usage = None
                    for p in t.panes:
                        p.usage = None
                w.refresh_titles()

    def set_language(self, code):
        if not code or code == self.app.config.get("language", "auto"):
            return
        self.app.config["language"] = code
        self.app.save_config()
        # Los textos se eligen al arrancar: una actualización en caliente los cambia
        # sin cerrar ninguna terminal, y Preferencias se vuelve a abrir donde estaba.
        GLib.idle_add(lambda: self.app.hot_reload(reopen="general") and False)

    # -- atajos
    def build_shortcuts(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
        self.hint = Gtk.Label(xalign=0, wrap=True)
        self.hint.get_style_context().add_class("dim-label")
        self.set_hint()
        outer.pack_start(self.hint, False, False, 0)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.get_style_context().add_class("frame")
        self.buttons = {}
        for action, label, _d in ACTIONS:
            btn = Gtk.Button(valign=Gtk.Align.CENTER)
            btn.connect("clicked", lambda b, a=action: self.start_capture(a, b))
            reset = Gtk.Button.new_from_icon_name("edit-undo-symbolic", Gtk.IconSize.BUTTON)
            reset.set_relief(Gtk.ReliefStyle.NONE)
            reset.set_tooltip_text(T("Volver al atajo original"))
            reset.set_valign(Gtk.Align.CENTER)
            reset.connect("clicked", lambda _b, a=action: self.reset(a))
            controls = Gtk.Box(spacing=4)
            controls.pack_start(btn, False, False, 0)
            controls.pack_start(reset, False, False, 0)
            self.buttons[action] = (btn, reset)
            lb.add(self.row(label, None, controls))
        fixed = Gtk.Label(label="Alt+1 … Alt+9", xalign=1)
        fixed.get_style_context().add_class("dim-label")
        lb.add(self.row(T("Ir a la pestaña 1 a 9"), None, fixed))
        self.refresh_buttons()
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add(lb)
        outer.pack_start(sc, True, True, 0)
        reset_all = Gtk.Button(label=T("Restablecer todos los atajos"), halign=Gtk.Align.END)
        reset_all.connect("clicked", lambda *_: self.reset(None))
        outer.pack_start(reset_all, False, False, 0)
        return outer

    def set_hint(self, text=None):
        self.hint.set_markup("<small>" + GLib.markup_escape_text(text or T(
            "Haga clic en un atajo y presione la combinación nueva. "
            "Esc cancela, Retroceso lo deja sin atajo.")) + "</small>")

    def refresh_buttons(self):
        for action, (btn, reset) in self.buttons.items():
            accels = self.app.config["shortcuts"].get(action) or []
            if self.capturing and self.capturing[0] == action:
                btn.set_label(T("Presione la combinación…"))
            else:
                btn.set_label(" · ".join(accel_label(a) for a in accels[:2]) or T("Sin atajo"))
            extra = accels[2:]
            btn.set_tooltip_text((T("También: ") + ", ".join(accel_label(a) for a in extra)) if extra else None)
            reset.set_sensitive(accels != DEFAULT_SHORTCUTS[action])

    def start_capture(self, action, btn):
        self.capturing = (action, btn)
        self.refresh_buttons()
        self.set_hint(T("Presione el atajo nuevo para «{action}».", action=ACTION_LABELS[action]))

    def reset(self, action):
        targets = [action] if action else list(DEFAULT_SHORTCUTS)
        for a in targets:
            self.app.config["shortcuts"][a] = list(DEFAULT_SHORTCUTS[a])
        self.capturing = None
        self.app.save_config()
        self.refresh_buttons()
        self.set_hint()

    def on_key(self, _w, event):
        if not self.capturing:
            if event.keyval == Gdk.KEY_Escape:
                self.destroy()
                return True
            return False
        if event.is_modifier or Gdk.keyval_name(event.keyval) in MODIFIER_KEYS:
            return True
        action, _btn = self.capturing
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        keyval = Gdk.keyval_to_lower(event.keyval)
        self.capturing = None
        if keyval == Gdk.KEY_Escape and not mods:
            self.set_hint()
        elif keyval == Gdk.KEY_BackSpace and not mods:
            self.app.config["shortcuts"][action] = []
            self.app.save_config()
            self.set_hint()
        else:
            accel = Gtk.accelerator_name(keyval, mods)
            key = accel_key(accel)
            msg = None
            for other, accels in self.app.config["shortcuts"].items():
                if other != action and any(accel_key(a) == key for a in accels):
                    self.app.config["shortcuts"][other] = [a for a in accels if accel_key(a) != key]
                    msg = T("{key} se lo quitó a «{action}».", key=accel_label(accel), action=ACTION_LABELS[other])
            self.app.config["shortcuts"][action] = [accel]
            self.app.save_config()
            self.set_hint(msg)
        self.refresh_buttons()
        return True


# ---------------------------------------------------------------- aplicación

class HiloApp(Gtk.Application):
    def __init__(self, fresh=False, relevo=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.relevo_in = relevo      # archivo con las terminales que deja la versión anterior
        self.relevo_out = False      # True mientras se arma ese archivo
        self.code_path = os.path.realpath(__file__)
        self.code_mtime = self.file_mtime()
        self.update_ready = False
        self.set_property("register-session", True)
        self.fresh = fresh
        self.windows = []
        self.config = load_config()
        self.profile = None
        self.font_scale = 1.0
        self.frozen = False        # apagando: no tocar más el disco
        self.hold_until = 0.0
        self.save_pending = False
        self.last_state_json = None
        self.saved_at = None
        self.last_sizes = {}
        self.started = False
        self.history = load_history()
        self.dragging = None        # pestaña que se está arrastrando
        self.dragging_pane = None   # panel que se está arrastrando desde su asa
        self.pending_dock = None
        self.in_transit = {}        # pestañas que GTK está pasando de una ventana a otra
        self.keymap = {}
        self.prefs = None
        self.help_win = None
        self.system_text, self.system_hot, self._sys_prev = None, False, None
        self.tts = None
        self.rebuild_keymap()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            frame.hilo-pane > border { border: none; }
            frame.hilo-pane.multi > border { border: 2px solid alpha(@theme_fg_color, 0.07); }
            frame.hilo-pane.multi.focused > border { border-color: #ff9f5a; }
            label.hilo-usage { font-size: 0.82em; font-feature-settings: "tnum"; }
            notebook.hilo-tabs > header.top > tabs > tab:checked {
                box-shadow: inset 0 -3px #ff9f5a; }
            button.hilo-update { background: #ff9f5a; color: #1e140e; border: none; box-shadow: none;
                                 padding: 0 10px; min-height: 22px; border-radius: 11px; font-weight: bold; }
            button.hilo-update:hover { background: #ffb27a; }
            label.hilo-sys { font-size: 0.9em; font-feature-settings: "tnum"; opacity: 0.75; }
            label.hilo-sys.hot { color: #ff7a59; opacity: 1; }
            button.hilo-tts.active { color: #ff9f5a; }
            progressbar.hilo-tts trough, progressbar.hilo-tts progress { min-height: 4px; border-radius: 2px; }
            progressbar.hilo-tts progress { background: #ff9f5a; border-color: #ff9f5a; }
            label.hilo-tts-on { color: #ff9f5a; }
            label.hilo-drag-hint { background: alpha(#1e140e, 0.88); color: #ffd9bd; border-radius: 12px;
                                   padding: 6px 14px; font-size: 0.9em; }
            label.hilo-grip { background: #ff9f5a; color: #1e140e; border-radius: 9px;
                              padding: 1px 10px; font-size: 0.85em; font-weight: bold; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.profile = gnome_profile()
        self.connect("query-end", self.on_query_end)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.on_signal, sig)
        GLib.timeout_add_seconds(self.config["state_interval_s"], self.tick_state)
        # `hilo --actualizar` desde otra terminal llega aquí.
        act = Gio.SimpleAction.new("relevo", None)
        act.connect("activate", lambda *_: self.hot_reload())
        self.add_action(act)
        GLib.timeout_add_seconds(self.config["scroll_interval_s"], self.tick_scroll)

    def do_activate(self):
        if self.started:
            self.new_window()
            return
        self.started = True
        state = None if self.fresh else self.load_state()
        if self.relevo_in:
            try:
                with open(self.relevo_in) as f:
                    state = json.load(f)
                state["saved_at"] = None
            except (OSError, ValueError) as e:
                print(T("hilo: no se pudo leer el relevo: {e}", e=e), file=sys.stderr)
            finally:
                try:
                    os.unlink(self.relevo_in)
                except OSError:
                    pass
        if tts_available(self.config):
            self.tts = Tts(self, state.get("tts") if self.relevo_in and state else None)
        if state and state.get("windows"):
            self.font_scale = state.get("font_scale", 1.0)
            self.saved_at = state.get("saved_at")
            for w in state["windows"]:
                self.spawn_window(w)
            if state.get("reopen_prefs"):
                page = state["reopen_prefs"]
                GLib.idle_add(lambda: self.show_preferences(page) and False)
        else:
            self.new_window()

    # -- ventanas
    def spawn_window(self, saved=None, empty=False):
        win = TermWindow(self, saved, empty)
        self.windows.append(win)
        self.refresh_tab_bars()
        win.show()
        cur = win.current_pane()
        if cur:
            cur.term.grab_focus()
        return win

    def new_window(self):
        self.spawn_window()
        self.request_save()

    def close_window(self, win):
        if len(self.windows) == 1:
            # Última ventana: se guarda tal cual para retomarla al abrir de nuevo.
            for p in win.panes():
                p.poll()
                p.dump_scroll()
            self.save_state(force=True)
            self.frozen = True
        win.closing = True
        for p in win.panes():
            p.kill()
        self.windows.remove(win)
        win.destroy()
        self.refresh_tab_bars()
        if not self.frozen:
            self.save_state(force=True)

    def window_emptied(self, win, user):
        win.closing = True
        if win in self.windows:
            self.windows.remove(win)
        win.destroy()
        self.refresh_tab_bars()
        if self.windows:
            self.request_save()
            return
        # Se salió de la última shell: sesión vacía, pero tras la pausa de seguridad.
        self.hold()
        def finish():
            if not self.frozen:
                self.save_state(force=True)
            self.release()
            return False
        GLib.timeout_add(2100 if not user else 50, finish)

    # -- apariencia
    def apply_look(self, term):
        p = self.profile
        font = Pango.FontDescription.from_string(p["font"])
        term.set_font(font)
        term.set_font_scale(self.font_scale)
        palette = [c for c in (rgba(x) for x in (p["palette"] or [])) if c]
        fg, bg = rgba(p["fg"]), rgba(p["bg"])
        if p["use_theme"]:
            ctx = Gtk.StyleContext()
            path = Gtk.WidgetPath()
            path.append_type(Gtk.TextView)
            ctx.set_path(path)
            ok_fg, tfg = ctx.lookup_color("theme_text_color")
            ok_bg, tbg = ctx.lookup_color("theme_base_color")
            if ok_fg and ok_bg:
                fg, bg = tfg, tbg
        term.set_colors(fg, bg, palette if len(palette) in (8, 16) else None)

    def zoom(self, factor):
        self.font_scale = 1.0 if factor is None else max(0.4, min(4.0, self.font_scale * factor))
        for p in self.all_panes():
            p.term.set_font_scale(self.font_scale)
        self.request_save()

    # -- atajos y preferencias
    def rebuild_keymap(self):
        self.keymap = {}
        for action, accels in self.config["shortcuts"].items():
            for a in accels:
                k = accel_key(a)
                if k:
                    self.keymap[k] = action

    def toggle_tabs(self):
        self.config["tabs_hidden"] = not self.config["tabs_hidden"]
        self.save_config()
        self.refresh_tab_bars()

    def save_config(self):
        self.rebuild_keymap()
        for w in self.windows:
            w.update_bar_tooltips()
        try:
            save_config(self.config)
        except OSError as e:
            print(T("hilo: no se pudo guardar la configuración: {e}", e=e), file=sys.stderr)

    def show_preferences(self, page=None):
        if self.prefs is None:
            self.prefs = PreferencesWindow(self)
            self.prefs.connect("destroy", lambda *_: setattr(self, "prefs", None))
        parent = self.get_active_window()
        if parent and parent is not self.prefs:
            self.prefs.set_transient_for(parent)
        if page:
            self.prefs.stack.set_visible_child_name(page)
        self.prefs.present()

    def show_help(self):
        # Se arma cada vez para mostrar los atajos vigentes.
        if self.help_win is not None:
            self.help_win.destroy()
        parent = self.get_active_window()
        self.help_win = HelpWindow(self)
        self.help_win.connect("destroy", lambda w: self.help_win is w and setattr(self, "help_win", None))
        if parent:
            self.help_win.set_transient_for(parent)
        self.help_win.present()

    def refresh_tab_bars(self):
        for w in self.windows:
            w.update_tabs_visibility()

    def pane_drag_begin(self, pane):
        self.dragging_pane = pane     # el asa sigue a la vista: ocultarla cortaría el arrastre
        self.refresh_tab_bars()
        self.show_drag_hints()

    def drag_hint(self):
        if self.dragging_pane is not None:
            return T("Espere sobre una pestaña para abrirla · Suelte en un panel para moverlo ahí · "
                     "En la barra queda como pestaña · Fuera de la ventana, en una nueva")
        if self.dragging is not None:
            return T("Espere sobre una pestaña para abrirla · Suelte en el borde de un panel para unirla · "
                     "En la barra, para reordenar")
        return None

    def show_drag_hints(self):
        text = self.drag_hint()
        for w in self.windows:
            for t in w.tabs:
                t.set_hint(text)

    def pane_drag_end(self):
        pane, self.dragging_pane = self.dragging_pane, None
        for w in self.windows:
            w.cancel_spring()
            w.update_tabs_visibility()
            for t in w.tabs:
                t.set_drop(None)
                t.set_hint(None)
        if pane:
            pane.set_grip(False, hover=False)

    def pane_to_new_window(self, pane):
        if pane.tab not in pane.win.tabs or len(pane.tab.panes) < 2:
            return
        w, h = pane.win.get_size()
        win = self.spawn_window({"width": w, "height": h}, empty=True)
        ptr = Gdk.Display.get_default().get_default_seat().get_pointer().get_position()
        win.move(max(ptr[1] - 40, 0), max(ptr[2] - 20, 0))
        pane.tab.detach_pane(pane, win, 0)

    def run_pending_dock(self):
        if self.pending_dock:
            target, src, pane, side = self.pending_dock
            self.pending_dock = None
            self.dragging = None
            if target in target.win.tabs and src in src.win.tabs:
                target.dock(src, pane, side)
        return False

    def all_panes(self):
        return [p for w in self.windows for p in w.panes()]

    # -- consumo por pestaña
    def sample_usage(self):
        now = time.monotonic()
        procs = scan_procs()
        children = {}
        for pid, (ppid, *_r) in procs.items():
            children.setdefault(ppid, []).append(pid)
        dt = now - getattr(self, "_usage_t", now)
        prev = getattr(self, "_usage_ticks", {})
        ticks_now = {}
        own = pss_kb(os.getpid(), 0)
        for w in self.windows:
            for t in w.tabs:
                all_rows = []
                for p in t.panes:
                    if not p.pid or p.pid not in procs:
                        p.usage = None
                        continue
                    stack, rows = [p.pid], []
                    while stack:
                        pid = stack.pop()
                        stack.extend(children.get(pid, ()))
                        _pp, ticks, rss, name = procs[pid]
                        ticks_now[pid] = ticks
                        cpu = (ticks - prev[pid]) / CLK_TCK / dt * 100 if pid in prev and dt > 0 else 0.0
                        rows.append((pss_kb(pid, rss), max(cpu, 0.0), name))
                    p.usage = (sum(r[0] for r in rows), sum(r[1] for r in rows), rows)
                    all_rows += rows
                if not all_rows:
                    t.usage = None
                    continue
                # Primero lo que está usando CPU (de más a menos); a igual CPU, la RAM.
                all_rows.sort(key=lambda r: (round(r[1], 1), r[0]), reverse=True)
                lines = [f"{fmt_mem(r[0]):>8}  {fmt_cpu(r[1]):>6}  {r[2]}" for r in all_rows[:6]]
                if len(all_rows) > 6:
                    lines.append(T("… y {n} procesos más", n=len(all_rows) - 6))
                if len(t.panes) > 1:
                    lines.append("")
                    for i, p in enumerate(t.leaf_order(), 1):
                        if p.usage:
                            lines.append(f"{T('Panel {i}', i=i)}: {fmt_mem(p.usage[0])} · {fmt_cpu(p.usage[1])}  ({p.title()})")
                lines.append("\n" + T("Hilo (toda la app): {mem}", mem=fmt_mem(own)))
                t.usage = (sum(r[0] for r in all_rows), sum(r[1] for r in all_rows), "\n".join(lines))
        self._usage_t, self._usage_ticks = now, ticks_now

    # -- historial de Claude
    def claude_seen(self, sid, argv, cwd, name, tab_id):
        now = time.time()
        e = next((x for x in self.history if x["id"] == sid), None)
        if e is None:
            e = {"id": sid, "started": now, "prompt": None}
            self.history.append(e)
        changed = e.get("ended") is not None or e.get("argv") != argv or e.get("cwd") != cwd
        e.update({"argv": argv, "cwd": cwd, "name": name, "tab": tab_id, "ended": None})
        if not e.get("prompt"):
            e["prompt"] = claude_first_prompt(sid)
            changed |= bool(e["prompt"])
        # last_seen se escribe a lo sumo cada minuto para no gastar disco.
        if changed or now - e.get("last_seen", 0) > 60:
            e["last_seen"] = now
            self.save_history()

    def claude_ended(self, sid):
        e = next((x for x in self.history if x["id"] == sid), None)
        if e and not self.frozen:
            e["ended"] = e["last_seen"] = time.time()
            if not e.get("prompt"):
                e["prompt"] = claude_first_prompt(sid)
            self.save_history()

    def recent_claude(self, n):
        return sorted(self.history, key=lambda e: e.get("last_seen") or 0, reverse=True)[:n]

    def save_history(self):
        if self.frozen:
            return
        self.history = self.recent_claude(HISTORY_MAX)
        try:
            atomic_write(HISTORY_FILE, json.dumps(self.history, indent=1, ensure_ascii=False).encode())
        except OSError as e:
            print(T("hilo: no se pudo guardar el historial: {e}", e=e), file=sys.stderr)

    # -- guardado
    def hold_saves(self, seconds):
        self.hold_until = max(self.hold_until, time.monotonic() + seconds)

    def request_save(self):
        if self.save_pending or self.frozen:
            return
        self.save_pending = True

        def run():
            self.save_pending = False
            self.save_state()
            return False
        GLib.timeout_add(400, run)

    def file_mtime(self):
        try:
            return os.stat(self.code_path).st_mtime
        except OSError:
            return None

    def hot_reload(self, reopen=None):
        """Actualización en caliente: el proceso se reemplaza por la versión nueva
        (exec) y le pasa cada pty abierto. El PID no cambia, así que las shells
        siguen siendo hijas de Hilo y nada se cierra."""
        try:
            with open(self.code_path, encoding="utf-8") as f:
                compile(f.read(), self.code_path, "exec")
        except (OSError, SyntaxError) as e:
            self.show_error(T("La versión nueva de Hilo tiene un error y no se aplicó."), str(e))
            return
        for p in self.all_panes():
            try:
                p.dump_scroll(force=True)
            except OSError:
                pass
        self.save_state(force=True)    # por si algo falla: queda como tras un apagón
        self.relevo_out = True
        try:
            state = {"version": 1, "font_scale": self.font_scale,
                     "windows": [w.serialize() for w in self.windows]}
            if reopen:
                state["reopen_prefs"] = reopen
            if self.tts:
                state["tts"] = self.tts.handover()
        finally:
            self.relevo_out = False
        path = os.path.join(STATE_DIR, "relevo.json")
        atomic_write(path, json.dumps(state, ensure_ascii=False).encode())
        self.frozen = True
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, self.code_path, "--relevo", path])

    def show_error(self, text, detail):
        d = Gtk.MessageDialog(transient_for=self.get_active_window(), modal=True,
                              message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=text)
        d.format_secondary_text(detail[:600])
        d.run()
        d.destroy()

    def tick_state(self):
        if self.frozen:
            return True
        changed = False
        for p in self.all_panes():
            changed |= p.poll()
        if self.config["show_usage"]:
            self.sample_usage()
        if self.config["show_system"]:
            self.system_text, self.system_hot, self._sys_prev = system_sample(self._sys_prev)
        if not self.update_ready and self.file_mtime() != self.code_mtime:
            self.update_ready = True
            for w in self.windows:
                w.update_update_btn()
        for w in self.windows:
            w.refresh_titles()
            w.update_system()
        self.save_state()
        return True

    def tick_scroll(self):
        if self.frozen or time.monotonic() < self.hold_until:
            return True
        for p in self.all_panes():
                try:
                    p.dump_scroll()
                except OSError as e:
                    print(T("hilo: no se pudo guardar el texto: {e}", e=e), file=sys.stderr)
        return True

    def save_state(self, force=False):
        if self.frozen:
            return
        if not force and time.monotonic() < self.hold_until:
            GLib.timeout_add(int((self.hold_until - time.monotonic()) * 1000) + 50,
                             lambda: self.save_state() and False)
            return
        state = {"version": 1, "font_scale": self.font_scale,
                 "windows": [w.serialize() for w in self.windows]}
        body = json.dumps(state, sort_keys=True)
        if body == self.last_state_json:
            return
        # Las pestañas nuevas o restauradas que nunca guardaron texto lo hacen ya.
        live_ids = {p.id for p in self.all_panes()}
        try:
            state["saved_at"] = time.time()
            atomic_write(STATE_FILE, json.dumps(state, indent=1, ensure_ascii=False).encode())
            self.last_state_json = body
        except OSError as e:
            print(T("hilo: no se pudo guardar la sesión: {e}", e=e), file=sys.stderr)
            return
        self.gc_scroll(live_ids)

    def gc_scroll(self, live_ids):
        try:
            for name in os.listdir(SCROLL_DIR):
                tid = name.split(".", 1)[0]
                if tid not in live_ids:
                    os.unlink(os.path.join(SCROLL_DIR, name))
        except OSError:
            pass

    def load_state(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def on_signal(self, sig):
        self.shutdown_save()
        self.quit()
        return False

    def on_query_end(self, *_):
        # Cierre de sesión o apagado ordenado: guardar todo y congelar.
        self.shutdown_save()

    def shutdown_save(self):
        if self.frozen:
            return
        if time.monotonic() >= self.hold_until:
            for p in self.all_panes():
                try:
                    p.dump_scroll()
                except OSError:
                    pass
            self.save_state(force=True)
        self.frozen = True

    def do_shutdown(self):
        if self.tts:
            self.tts.shutdown()
        if not self.frozen and time.monotonic() >= self.hold_until and self.windows:
            self.save_state(force=True)
        Gtk.Application.do_shutdown(self)


def main():
    args = sys.argv[1:]
    if "--tts-hook" in args:
        return tts_hook()
    if "--tts-register" in args:
        print(T("Hook de lectura en voz alta registrado.") if tts_register_hook()
              else T("El hook de lectura en voz alta ya estaba registrado."))
        return 0
    if "-h" in args or "--help" in args:
        print(T("USO", state=STATE_FILE, history=HISTORY_FILE, config=CONFIG_FILE))
        return 0
    # Cada opción tiene su nombre en español y en inglés.
    for es, en in (("--historial", "--history"), ("--cerradas", "--closed"), ("--actualizar", "--update")):
        args = [es if a == en else a for a in args]
    if "--historial" in args:
        i = args.index("--historial")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 20
        hist = sorted(load_history(), key=lambda e: e.get("last_seen") or 0, reverse=True)[:n]
        if not hist:
            print(T("Todavía no hay sesiones de Claude registradas."))
        for e in reversed(hist):
            state = T("abierta") if not e.get("ended") else T("cerrada")
            print(f"\033[1m{history_label(e, 110)}\033[0m  [{state}]")
            argv = claude_resume_argv(e.get("argv") or ["claude"], e["id"])
            print(f"  cd {shlex.quote(e.get('cwd') or HOME)} && {shlex.join(argv)}\n")
        return 0
    if "--cerradas" in args:
        closed = list_closed()
        if not closed:
            print(T("No hay paneles cerrados guardados."))
        for c in reversed(closed):
            when = time.strftime("%d %b %H:%M", time.localtime(c["closed_at"]))
            print(f"\033[1m{when} · {c.get('title') or pretty_dir(c.get('cwd'))}\033[0m")
            for sid in c.get("claude_ids") or []:
                print(f"  claude --resume {sid}")
            print(f"  {T('texto')}: {c['_txt']}\n")
        return 0
    if "--actualizar" in args:
        # Pide a la Hilo abierta que aplique la versión nueva sin cerrar nada.
        client = Gio.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        client.register(None)
        if not client.get_is_remote():
            print(T("Hilo no está abierta."))
            return 1
        client.activate_action("relevo", None)
        client.get_dbus_connection().flush_sync(None)
        print(T("Listo: Hilo se actualizó sin cerrar nada."))
        return 0
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Hilo")
    Gtk.Window.set_default_icon_name(APP_ID)
    relevo = args[args.index("--relevo") + 1] if "--relevo" in args and args.index("--relevo") + 1 < len(args) else None
    app = HiloApp(fresh="--fresh" in args, relevo=relevo)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
