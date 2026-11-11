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


# ---------------------------------------------------------------- utilidades

# (acción, descripción, atajos por defecto en formato GTK)
ACTIONS = [
    ("new_tab", "Nueva pestaña", ["<Control>t", "<Control><Shift>t"]),
    ("new_window", "Nueva ventana", ["<Control><Shift>n"]),
    ("split_right", "Dividir panel a la derecha", ["<Control><Shift>e"]),
    ("split_down", "Dividir panel hacia abajo", ["<Control><Shift>o"]),
    ("close_pane", "Cerrar panel (o la pestaña si es el único)", ["<Control><Shift>w"]),
    ("zoom_pane", "Maximizar / restaurar panel", ["<Control><Shift>x"]),
    ("detach_pane", "Mover panel a una pestaña nueva", []),
    ("focus_left", "Ir al panel de la izquierda", ["<Alt>Left"]),
    ("focus_right", "Ir al panel de la derecha", ["<Alt>Right"]),
    ("focus_up", "Ir al panel de arriba", ["<Alt>Up"]),
    ("focus_down", "Ir al panel de abajo", ["<Alt>Down"]),
    ("close_tab", "Cerrar pestaña completa", []),
    ("close_window", "Cerrar ventana", ["<Control><Shift>q"]),
    ("copy", "Copiar", ["<Control><Shift>c"]),
    ("paste", "Pegar", ["<Control><Shift>v"]),
    ("search", "Buscar", ["<Control><Shift>f"]),
    ("next_tab", "Pestaña siguiente", ["<Control>Page_Down"]),
    ("prev_tab", "Pestaña anterior", ["<Control>Page_Up"]),
    ("move_tab_right", "Mover pestaña a la derecha", ["<Control><Shift>Page_Down"]),
    ("move_tab_left", "Mover pestaña a la izquierda", ["<Control><Shift>Page_Up"]),
    ("rename_tab", "Renombrar pestaña", []),
    ("zoom_in", "Agrandar texto", ["<Control>plus", "<Control>equal", "<Control>KP_Add", "<Control><Shift>plus"]),
    ("zoom_out", "Achicar texto", ["<Control>minus", "<Control>KP_Subtract"]),
    ("zoom_reset", "Tamaño normal", ["<Control>0", "<Control>KP_0"]),
    ("toggle_tabs", "Ocultar / mostrar las pestañas", ["<Control><Shift>h"]),
    ("fullscreen", "Pantalla completa", ["F11"]),
    ("preferences", "Preferencias", ["<Control>comma"]),
    ("show_help", "Ver atajos y controles", ["<Control><Shift>question"]),
]

# Cómo se agrupan las acciones en la ventana de atajos y controles.
HELP_GROUPS = [
    ("Pestañas", ["new_tab", "close_tab", "next_tab", "prev_tab", "move_tab_right", "move_tab_left",
                  "rename_tab", "toggle_tabs"]),
    ("Paneles", ["split_right", "split_down", "focus_left", "focus_right", "focus_up", "focus_down",
                 "zoom_pane", "detach_pane", "close_pane"]),
    ("Texto", ["copy", "paste", "search", "zoom_in", "zoom_out", "zoom_reset"]),
    ("Ventana", ["new_window", "close_window", "fullscreen", "preferences", "show_help"]),
]
# Lo que no se configura: teclas fijas y gestos con el mouse.
HELP_FIXED = [
    ("Ir a la pestaña 1 a 9", "Alt+1 … Alt+9"),
    ("Seleccionar texto", "Arrastrar con el mouse"),
    ("Seleccionar dentro de un programa que usa el mouse (Claude, vim)", "Shift + arrastrar"),
    ("Seleccionar palabra / línea", "Doble clic / triple clic"),
    ("Abrir un enlace", "Ctrl + clic"),
    ("Menú del panel", "Clic derecho"),
    ("Renombrar pestaña", "Doble clic en la pestaña"),
    ("Volver una pestaña panel", "Arrastrar la pestaña sobre la terminal"),
    ("Pasar una pestaña a otra ventana", "Arrastrarla a la otra ventana"),
    ("Abrir una pestaña en ventana nueva", "Soltarla fuera de toda ventana"),
    ("Cambiar el tamaño de los paneles", "Arrastrar la línea divisoria"),
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
        return f"{mb / 1024:.1f} GB".replace(".", ",")
    return f"{mb:.0f} MB"


def fmt_cpu(pct):
    return f"{pct:.0f} %" if pct >= 10 or pct == 0 else f"{pct:.1f} %".replace(".", ",")


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
        self.widget.add(inner)
        self.widget.show_all()

        self.restore_cmd = saved.get("command")
        self.restore_scroll = saved.get("id") and os.path.join(SCROLL_DIR, f"{saved['id']}.txt")
        self.saved_at = saved.get("_saved_at")

    @property
    def win(self):
        return self.tab.win

    # -- arranque
    def start(self):
        if self.restore_scroll and os.path.exists(self.restore_scroll):
            try:
                with open(self.restore_scroll, encoding="utf-8", errors="replace") as f:
                    text = f.read().rstrip()
                if text:
                    when = time.strftime("%d %b %H:%M", time.localtime(self.saved_at)) if self.saved_at else ""
                    self.term.feed((text.replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n").encode())
                    self.term.feed(f"\x1b[2m──── sesión restaurada {when} ────\x1b[0m\r\n".encode())
                if self.consume_scroll:
                    for p in (self.restore_scroll, self.restore_scroll[:-4] + ".json"):
                        if os.path.exists(p):
                            os.unlink(p)
            except OSError:
                pass
        self.term.spawn_async(
            Vte.PtyFlags.DEFAULT, self.cwd, [user_shell()], child_env(),
            GLib.SpawnFlags.DEFAULT, None, None, -1, None, self.on_spawned, None)

    def on_spawned(self, term, pid, error, *_):
        if error or pid <= 0:
            term.feed(f"\r\nNo se pudo abrir la shell: {error}\r\n".encode())
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
        return {"id": self.id, "cwd": self.cwd, "command": self.cmd, "claude_ids": self.claude_ids[-20:]}

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
            print(f"hilo: no se pudo archivar el panel: {e}", file=sys.stderr)

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

    def request_close(self):
        """Cierra solo este panel (si es el único, se cierra la pestaña)."""
        if self.foreground_running() and not self.win.confirm(
                "Hay un proceso corriendo en este panel.", "Cerrar panel"):
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
        self.widget.drag_dest_set(Gtk.DestDefaults(0), [TAB_TARGET], Gdk.DragAction.MOVE)
        self.widget.connect("drag-motion", self.on_drag_motion)
        self.widget.connect("drag-leave", lambda *_: self.set_drop(None))
        self.widget.connect("drag-drop", self.on_drag_drop)
        self.widget.show_all()
        focus_idx = saved.get("focused", 0)
        leaves = self.leaf_order()
        self.focused = leaves[focus_idx] if 0 <= focus_idx < len(leaves) else leaves[0]

        self.label = Gtk.Label(label="…", ellipsize=Pango.EllipsizeMode.END, width_chars=12, max_width_chars=28)
        self.count_label = Gtk.Label()
        self.count_label.get_style_context().add_class("dim-label")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Cerrar pestaña (todos sus paneles)")
        close.connect("clicked", lambda *_: self.request_close())
        self.usage_label = Gtk.Label(xalign=1)
        self.usage_label.get_style_context().add_class("dim-label")
        self.tab_box = Gtk.Box(spacing=4)
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

    def detach_pane(self, pane):
        """Mueve el panel a una pestaña nueva, con su shell viva."""
        if len(self.panes) < 2 or pane not in self.panes:
            return
        self.unzoom()
        self.unlink(pane)
        if self.focused is pane:
            self.focused = self.leaf_order()[0]
        tab = Tab(self.win, adopt=pane)
        self.win.nb.set_current_page(self.win.add_tab(tab, after_current=True, start=False))
        GLib.idle_add(lambda: pane.term.grab_focus() and False)
        self.win.refresh_titles()
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
    def drop_target(self, x, y):
        best = None
        for p in self.panes:
            if not p.widget.get_mapped():
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
        src = self.app.dragging
        if src is None or src is self or widget.drag_dest_find_target(ctx, None) is None:
            self.set_drop(None)
            Gdk.drag_status(ctx, 0, time_)
            return False
        self.set_drop(self.drop_target(x, y))
        Gdk.drag_status(ctx, Gdk.DragAction.MOVE if self.drop else 0, time_)
        return True

    def on_drag_drop(self, _w, ctx, x, y, time_):
        src, drop = self.app.dragging, self.drop_target(x, y)
        self.set_drop(None)
        ok = src is not None and src is not self and drop is not None
        Gtk.drag_finish(ctx, ok, False, time_)
        if ok:
            # El cuaderno todavía está terminando el arrastre: se mueve después.
            self.app.pending_dock = (self, src, drop[0], drop[1])
            GLib.timeout_add(150, self.app.run_pending_dock)
        return True

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
                f"Hay {running} proceso(s) corriendo en esta pestaña.", "Cerrar pestaña"):
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
        # Mismo grupo en todas las ventanas: las pestañas se pueden pasar de una a otra.
        self.nb.set_group_name("hilo")
        self.nb.connect("drag-begin", self.on_tab_drag_begin)
        self.nb.connect_after("drag-end", self.on_tab_drag_end)
        self.nb.connect("page-added", self.on_page_added)
        self.nb.connect("page-removed", self.on_page_removed)
        self.nb.connect("create-window", self.on_create_window)
        self.nb.connect("switch-page", self.on_switch_page)
        self.nb.connect("page-reordered", lambda *_: self.app.request_save())
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
        # Consumo de todo el equipo (CPU, temperatura y RAM).
        self.sys_label = Gtk.Label(margin_end=10)
        self.sys_label.get_style_context().add_class("hilo-sys")
        self.sys_label.set_tooltip_text("Todo el equipo: uso de CPU, temperatura del procesador y RAM usada / total")
        bar_btns.pack_start(self.sys_label, False, False, 0)
        for b in (self.plus_btn, self.prefs_btn, self.help_btn, self.hide_btn):
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_focus_on_click(False)
            bar_btns.pack_start(b, False, False, 0)
        bar_btns.show_all()
        self.update_system()
        self.nb.set_action_widget(bar_btns, Gtk.PackType.END)
        self.update_bar_tooltips()

        self.search = Gtk.SearchEntry(placeholder_text="Buscar en el panel (Enter: anterior, Shift+Enter: siguiente)")
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

    def add_tab(self, tab, after_current=False, start=True):
        self.tabs.append(tab)
        pos = self.nb.get_current_page() + 1 if after_current else -1
        idx = self.nb.insert_page(tab.widget, tab.tab_box, pos)
        self.nb.set_tab_reorderable(tab.widget, True)
        self.nb.set_tab_detachable(tab.widget, True)
        self.update_tabs_visibility()
        if start:
            GLib.idle_add(lambda: tab.start() and False)
        return idx

    # -- arrastrar una pestaña para convertirla en panel
    def on_tab_drag_begin(self, *_):
        self.app.dragging = self.current()
        # Mientras se arrastra, todas las ventanas muestran su barra para poder soltar ahí.
        for w in self.app.windows:
            w.nb.set_show_tabs(True)
        prev = self.prev_tab
        # Al tomarla, GTK la muestra; se vuelve a la anterior para soltarla encima.
        if prev in self.tabs and prev is not self.app.dragging:
            GLib.idle_add(lambda: self.nb.set_current_page(self.nb.page_num(prev.widget)) and False)

    def on_tab_drag_end(self, *_):
        GLib.idle_add(self.clear_drag)

    def clear_drag(self):
        if not self.app.pending_dock:
            self.app.dragging = None
        for w in self.app.windows:
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
        self.nb.set_show_tabs(not self.app.config["tabs_hidden"] or bool(self.app.dragging))
        self.refresh_titles()

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
        self.plus_btn.set_tooltip_text(tip("Nueva pestaña", "new_tab"))
        self.prefs_btn.set_tooltip_text(tip("Preferencias", "preferences"))
        self.help_btn.set_tooltip_text(tip("Atajos y controles", "show_help"))
        self.hide_btn.set_tooltip_text(tip("Ocultar las pestañas", "toggle_tabs"))

    def refresh_titles(self):
        for t in self.tabs:
            multi = len(t.panes) > 1 and not t.zoomed
            cur = t.current_pane()
            for p in t.panes:
                ctx = p.widget.get_style_context()
                (ctx.add_class if multi else ctx.remove_class)("multi")
                (ctx.add_class if p is cur else ctx.remove_class)("focused")
            title = t.title()
            if t.label.get_text() != title:
                t.label.set_text(title)
            n = len(t.panes)
            if n > 1:
                t.count_label.set_markup(f"<small>{'⤢ ' if t.zoomed else ''}▦ {n}</small>")
                t.count_label.show()
            else:
                t.count_label.hide()
            tip = title + (f"\n{n} paneles" if n > 1 else "")
            if t.usage:
                ram, cpu, detail = t.usage
                text = f"{fmt_mem(ram)} · {fmt_cpu(cpu)}"
                t.usage_label.set_markup(f"<small>{GLib.markup_escape_text(text)}</small>")
                t.usage_label.show()
                tip = f"{tip}\n\n{detail}"
            else:
                t.usage_label.hide()
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
                    title += f"  ·  {len(cur.panes)} paneles"
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
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, action, Gtk.ResponseType.ACCEPT)
        d.set_default_response(Gtk.ResponseType.CANCEL)
        ok = d.run() == Gtk.ResponseType.ACCEPT
        d.destroy()
        return ok

    def rename_tab(self, tab):
        d = Gtk.Dialog(title="Renombrar pestaña", transient_for=self, modal=True)
        d.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Guardar", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.OK)
        e = Gtk.Entry(text=tab.custom_title or "", activates_default=True,
                      placeholder_text="Vacío = nombre automático")
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
            ("Copiar", "copy", lambda: pane.term.copy_clipboard_format(Vte.Format.TEXT), pane.term.get_has_selection()),
            ("Pegar", "paste", lambda: pane.term.paste_clipboard(), True),
            None,
            ("Dividir a la derecha", "split_right", lambda: tab.split(pane, "h"), True),
            ("Dividir hacia abajo", "split_down", lambda: tab.split(pane, "v"), True),
            ("Restaurar paneles" if tab.zoomed else "Maximizar panel", "zoom_pane", tab.toggle_zoom, multi),
            ("Mover panel a una pestaña nueva", "detach_pane", lambda: tab.detach_pane(pane), multi),
            ("Cerrar panel", "close_pane", pane.request_close, multi),
            None,
            ("Nueva pestaña", "new_tab", self.new_tab, True),
            ("Nueva ventana", "new_window", self.app.new_window, True),
            ("Renombrar pestaña…", "rename_tab", lambda: self.rename_tab(tab), True),
            ("Cerrar pestaña", "close_tab", tab.request_close, True),
            ("Mostrar las pestañas" if self.app.config["tabs_hidden"] else "Ocultar las pestañas",
             "toggle_tabs", self.app.toggle_tabs, True),
            None,
            ("Preferencias…", "preferences", self.app.show_preferences, True),
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
                ("Sesiones de Claude", hist, self.resume_claude, "Sin sesiones registradas"),
                ("Paneles cerrados", closed, self.reopen_closed, "No hay paneles cerrados")):
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
                f"Hay {running} proceso(s) corriendo en esta ventana. "
                "Si la cierra, se detienen; la sesión se recuerda para la próxima vez.",
                "Cerrar ventana"):
            return True
        self.app.close_window(self)
        return True


# ---------------------------------------------------------------- preferencias

class HelpWindow(Gtk.Window):
    """Lista de atajos (los vigentes, no los de fábrica) y gestos con el mouse."""

    def __init__(self, app):
        super().__init__(title="Atajos y controles", modal=False)
        self.app = app
        self.set_default_size(560, 680)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        hb = Gtk.HeaderBar(show_close_button=True, title="Atajos y controles")
        edit = Gtk.Button(label="Cambiar atajos…")
        edit.connect("clicked", lambda *_: (self.app.show_preferences("atajos"), self.destroy()))
        hb.pack_start(edit)
        self.set_titlebar(hb)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=18)
        shortcuts = self.app.config["shortcuts"]
        for title, actions in HELP_GROUPS:
            rows = []
            for a in actions:
                accels = shortcuts.get(a) or []
                keys = " o ".join(accel_label(x) for x in accels[:2]) if accels else "sin atajo"
                rows.append((ACTION_LABELS[a], keys, not accels))
            self.section(box, title, rows)
        self.section(box, "Teclas fijas y mouse", [(a, b, False) for a, b in HELP_FIXED])

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
        super().__init__(title="Preferencias de Hilo", modal=False)
        self.app = app
        self.set_default_size(560, 620)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.capturing = None   # (acción, botón) mientras espera la tecla nueva

        stack = self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER)
        hb = Gtk.HeaderBar(show_close_button=True, custom_title=switcher)
        self.set_titlebar(hb)
        stack.add_titled(self.build_general(), "general", "General")
        stack.add_titled(self.build_shortcuts(), "atajos", "Atajos")
        self.add(stack)
        self.connect("key-press-event", self.on_key)
        self.show_all()

    # -- general
    def build_general(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, margin=18)
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.get_style_context().add_class("frame")
        rows = [
            ("show_usage", "Mostrar RAM y CPU en las pestañas",
             "La suma de la shell y de todo lo que corre dentro."),
            ("show_system", "Mostrar CPU, temperatura y RAM del equipo",
             "En la barra de pestañas, para todo el equipo."),
            ("autorun_claude", "Reanudar Claude Code solo al restaurar",
             "Si se apaga, el comando queda escrito esperando Enter."),
        ]
        for key, title, sub in rows:
            sw = Gtk.Switch(active=bool(self.app.config[key]), valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda w, _p, k=key: self.set_option(k, w.get_active()))
            lb.add(self.row(title, sub, sw))
        entry = Gtk.Entry(text=", ".join(self.app.config["autorun"]), width_chars=22, valign=Gtk.Align.CENTER)
        entry.connect("changed", lambda e: self.set_option(
            "autorun", [x.strip() for x in e.get_text().split(",") if x.strip()]))
        lb.add(self.row("Programas que se vuelven a abrir solos",
                        "Separados por coma. El resto queda escrito esperando Enter.", entry))
        box.pack_start(lb, False, False, 0)
        info = Gtk.Label(xalign=0, wrap=True, margin_top=14)
        info.get_style_context().add_class("dim-label")
        info.set_markup(f"<small>La configuración se guarda en {GLib.markup_escape_text(pretty_dir(CONFIG_FILE))}</small>")
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
            reset.set_tooltip_text("Volver al atajo original")
            reset.set_valign(Gtk.Align.CENTER)
            reset.connect("clicked", lambda _b, a=action: self.reset(a))
            controls = Gtk.Box(spacing=4)
            controls.pack_start(btn, False, False, 0)
            controls.pack_start(reset, False, False, 0)
            self.buttons[action] = (btn, reset)
            lb.add(self.row(label, None, controls))
        fixed = Gtk.Label(label="Alt+1 … Alt+9", xalign=1)
        fixed.get_style_context().add_class("dim-label")
        lb.add(self.row("Ir a la pestaña 1 a 9", None, fixed))
        self.refresh_buttons()
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add(lb)
        outer.pack_start(sc, True, True, 0)
        reset_all = Gtk.Button(label="Restablecer todos los atajos", halign=Gtk.Align.END)
        reset_all.connect("clicked", lambda *_: self.reset(None))
        outer.pack_start(reset_all, False, False, 0)
        return outer

    def set_hint(self, text=None):
        self.hint.set_markup("<small>" + GLib.markup_escape_text(text or (
            "Haga clic en un atajo y presione la combinación nueva. "
            "Esc cancela, Retroceso lo deja sin atajo.")) + "</small>")

    def refresh_buttons(self):
        for action, (btn, reset) in self.buttons.items():
            accels = self.app.config["shortcuts"].get(action) or []
            if self.capturing and self.capturing[0] == action:
                btn.set_label("Presione la combinación…")
            else:
                btn.set_label(" · ".join(accel_label(a) for a in accels[:2]) or "Sin atajo")
            extra = accels[2:]
            btn.set_tooltip_text(("También: " + ", ".join(accel_label(a) for a in extra)) if extra else None)
            reset.set_sensitive(accels != DEFAULT_SHORTCUTS[action])

    def start_capture(self, action, btn):
        self.capturing = (action, btn)
        self.refresh_buttons()
        self.set_hint(f"Presione el atajo nuevo para «{ACTION_LABELS[action]}».")

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
                    msg = f"{accel_label(accel)} se lo quitó a «{ACTION_LABELS[other]}»."
            self.app.config["shortcuts"][action] = [accel]
            self.app.save_config()
            self.set_hint(msg)
        self.refresh_buttons()
        return True


# ---------------------------------------------------------------- aplicación

class HiloApp(Gtk.Application):
    def __init__(self, fresh=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
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
        self.pending_dock = None
        self.in_transit = {}        # pestañas que GTK está pasando de una ventana a otra
        self.keymap = {}
        self.prefs = None
        self.help_win = None
        self.system_text, self.system_hot, self._sys_prev = None, False, None
        self.rebuild_keymap()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            frame.hilo-pane > border { border: none; }
            frame.hilo-pane.multi > border { border: 2px solid alpha(@theme_fg_color, 0.07); }
            frame.hilo-pane.multi.focused > border { border-color: #ff9f5a; }
            label.hilo-sys { font-size: 0.9em; font-feature-settings: "tnum"; opacity: 0.75; }
            label.hilo-sys.hot { color: #ff7a59; opacity: 1; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.profile = gnome_profile()
        self.connect("query-end", self.on_query_end)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.on_signal, sig)
        GLib.timeout_add_seconds(self.config["state_interval_s"], self.tick_state)
        GLib.timeout_add_seconds(self.config["scroll_interval_s"], self.tick_scroll)

    def do_activate(self):
        if self.started:
            self.new_window()
            return
        self.started = True
        state = None if self.fresh else self.load_state()
        if state and state.get("windows"):
            self.font_scale = state.get("font_scale", 1.0)
            self.saved_at = state.get("saved_at")
            for w in state["windows"]:
                self.spawn_window(w)
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
            print(f"hilo: no se pudo guardar la configuración: {e}", file=sys.stderr)

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
                all_rows.sort(key=lambda r: r[0], reverse=True)
                lines = [f"{fmt_mem(r[0]):>8}  {fmt_cpu(r[1]):>6}  {r[2]}" for r in all_rows[:6]]
                if len(all_rows) > 6:
                    lines.append(f"… y {len(all_rows) - 6} procesos más")
                if len(t.panes) > 1:
                    lines.append("")
                    for i, p in enumerate(t.leaf_order(), 1):
                        if p.usage:
                            lines.append(f"Panel {i}: {fmt_mem(p.usage[0])} · {fmt_cpu(p.usage[1])}  ({p.title()})")
                lines.append(f"\nHilo (toda la app): {fmt_mem(own)}")
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
            print(f"hilo: no se pudo guardar el historial: {e}", file=sys.stderr)

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
                    print(f"hilo: no se pudo guardar el texto: {e}", file=sys.stderr)
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
            print(f"hilo: no se pudo guardar la sesión: {e}", file=sys.stderr)
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
        if not self.frozen and time.monotonic() >= self.hold_until and self.windows:
            self.save_state(force=True)
        Gtk.Application.do_shutdown(self)


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        print("Uso: hilo [--fresh | --historial [N] | --cerradas]\n\n"
              "  --fresh        abrir sin restaurar la sesión guardada\n"
              "  --historial N  últimas N sesiones de Claude con su comando para reanudar (20)\n"
              "  --cerradas     pestañas cerradas guardadas\n"
              f"\nSesión: {STATE_FILE}\nHistorial: {HISTORY_FILE}\nConfiguración: {CONFIG_FILE}")
        return 0
    if "--historial" in args:
        i = args.index("--historial")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 20
        hist = sorted(load_history(), key=lambda e: e.get("last_seen") or 0, reverse=True)[:n]
        if not hist:
            print("Todavía no hay sesiones de Claude registradas.")
        for e in reversed(hist):
            state = "abierta" if not e.get("ended") else "cerrada"
            print(f"\033[1m{history_label(e, 110)}\033[0m  [{state}]")
            argv = claude_resume_argv(e.get("argv") or ["claude"], e["id"])
            print(f"  cd {shlex.quote(e.get('cwd') or HOME)} && {shlex.join(argv)}\n")
        return 0
    if "--cerradas" in args:
        closed = list_closed()
        if not closed:
            print("No hay pestañas cerradas guardadas.")
        for c in reversed(closed):
            when = time.strftime("%d %b %H:%M", time.localtime(c["closed_at"]))
            print(f"\033[1m{when} · {c.get('title') or pretty_dir(c.get('cwd'))}\033[0m")
            for sid in c.get("claude_ids") or []:
                print(f"  claude --resume {sid}")
            print(f"  texto: {c['_txt']}\n")
        return 0
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Hilo")
    Gtk.Window.set_default_icon_name(APP_ID)
    app = HiloApp(fresh="--fresh" in args)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
