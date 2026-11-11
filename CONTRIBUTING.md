# Contributing

**English** · [Español](CONTRIBUTING.es.md)

Thanks for wanting to improve Hilo. This guide covers how to run it in development without touching your real session, how the code is organized, and what to check before sending a change.

## Requirements

- Linux with GNOME (or any GTK 3 desktop).
- Python 3.10 or newer.
- PyGObject with GTK 3 and VTE 2.91. On Ubuntu and Debian:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
```

There are no other dependencies: no `pip install`, no build step.

## Running it in development

Hilo is a single-instance application: if Hilo is already open and you run `./hilo.py`, the new process doesn't start another terminal. Instead, it asks the running one to open a window. It would also share the same saved session.

To test changes without mixing anything into your everyday Hilo, use a different application id and temporary folders:

```sh
export HILO_APP_ID=dev.hilo.Prueba
export XDG_STATE_HOME="$(mktemp -d)"
export XDG_CONFIG_HOME="$(mktemp -d)"
./hilo.py
```

- `HILO_APP_ID` makes the development copy a separate application.
- `XDG_STATE_HOME` isolates the session, saved text, Claude history and closed panes.
- `XDG_CONFIG_HOME` isolates preferences and shortcuts.

If you want to keep the test session across restarts (for example, to test restoring), use a fixed folder instead of `mktemp -d`.

`./hilo.py --fresh` opens without restoring anything. With the development copy open, `./hilo.py --update` (same environment variables) applies your edits without closing it.

To test without any window showing up on your desktop, run it on a virtual display: `Xvfb :97 &` and then `DISPLAY=:97 GDK_BACKEND=x11 ./hilo.py`.

## How it's organized

Everything lives in `hilo.py`, on purpose: it installs with a symlink and there are no packages to maintain.

| Piece | What it does |
|---|---|
| Top-level functions | Settings, atomic writes, reading `/proc` (command, directory, usage) and everything related to Claude Code (finding the session, building the `--resume` command). |
| `Pane` | One terminal. Starts the shell, restores saved text, detects what's running and knows how to describe itself for saving. |
| `Tab` | One tab. Holds a tree of panes (nested `Gtk.Paned`): split, close, move focus, maximize and accept dragged tabs. |
| `TermWindow` | A window with its tab bar, search, context menu and shortcuts. |
| `PreferencesWindow` | Preferences: general options and shortcut capture. |
| `HelpWindow` | The shortcuts and controls window: current shortcuts, fixed keys and mouse gestures. |
| `HiloApp` | The application. Saves the session every few seconds, restores it on launch, measures usage, keeps the Claude history and does the final save on shutdown. |

### Session format

`session.json` holds a list of windows, and each window a list of tabs. The content of each tab is a tree:

```json
{ "split": "h", "ratio": 0.5, "children": [
    { "pane": { "id": "…", "cwd": "/path", "command": { "argv": ["nvim"] }, "claude_ids": [] } },
    { "pane": { "id": "…", "cwd": "/path", "command": null, "claude_ids": [] } }
] }
```

If you change this format, make sure a session saved by the previous version still opens. Someone may update Hilo right after a power cut.

### Rules not to break

- **Every write to disk goes through `atomic_write`.** A power cut in the middle of a write must never leave a corrupt file.
- **Nothing risky runs on its own.** On restore, only Claude and the `autorun` list are relaunched. Any other command is typed at the prompt and waits for Enter.
- **The final save on shutdown must not be lost.** If you touch signal handling (`SIGTERM`, `SIGHUP`) or how child processes are closed, check that the session is still complete after a shutdown.
- **Hot updates must keep working.** `hot_reload` hands the running state (including each pane's `relevo` data: pty fd, shell pid, screen text) to the new code via `exec`. The new version must still accept what the previous one serialized.
- **Keep it light.** Hilo watches processes every few seconds. No heavy work in those timers.

## Testing a change

There's no automated test suite yet. Before sending a change, check what you touched by hand, always with the development copy:

- **Restore:** open tabs and panes in different directories, with something running, and kill the process with `kill -9`. When you open it again, everything should come back the same.
- **Clean shutdown:** close it with `kill -TERM`. The session should be saved with the commands that were running.
- **Quitting on purpose:** type `exit` in every tab. When you open it again, nothing should be restored.
- **Panes and dragging:** split, close, maximize, and drag tabs within the window, to another window and outside any window.
- **Shortcuts:** change a shortcut in Preferences, restart and check that it sticks.

At the very least, the file has to compile:

```sh
python3 -m py_compile hilo.py
```

## Conventions

- The interface comes in English and Spanish. In the code, every text is written in Spanish inside `T("…")`, and its English version goes in the `EN` dictionary at the top of `hilo.py`. A new text without its English entry shows up in Spanish for English users. Write both in plain natural language; the Spanish uses the formal "usted".
- Documentation comes in two versions: English (`README.md`, `CONTRIBUTING.md`) and Spanish (`README.es.md`, `CONTRIBUTING.es.md`). If you change one, update the other.
- Clear code over clever code. Comments explain why, not what.
- Don't add dependencies beyond Python, GTK and VTE without discussing it first.
- One change per pull request, with a commit message that says what changes and why.

## Sending a change

1. Fork the repository and create a branch from `main`.
2. Make your change and test it as described above.
3. Open a pull request explaining what changed, why, and how you tested it.

`main` is protected: changes only go in through pull requests approved by the maintainer.

By contributing, you agree that your contribution is published under the project's MIT license.

If you find a bug, open an issue with steps to reproduce it and, if relevant, the contents of `session.json` with paths and personal data removed.
