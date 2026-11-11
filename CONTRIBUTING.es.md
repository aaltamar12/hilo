# Cómo contribuir

[English](CONTRIBUTING.md) · **Español**

Gracias por querer mejorar Hilo. Esta guía explica cómo correrla en desarrollo sin tocar su sesión real, cómo está organizado el código y qué revisar antes de proponer un cambio.

## Requisitos

- Linux con GNOME (o cualquier escritorio con GTK 3).
- Python 3.10 o más reciente.
- PyGObject con GTK 3 y VTE 2.91. En Ubuntu y Debian:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
```

No hay más dependencias, ni `pip install`, ni paso de compilación.

## Correrla en desarrollo

Hilo es una aplicación de instancia única: si ya tiene Hilo abierta y ejecuta `./hilo.py`, el proceso nuevo no arranca otra terminal, sino que le pide a la que está abierta que abra una ventana. Además, compartiría la misma sesión guardada.

Para probar cambios sin mezclar nada con su Hilo de todos los días, use otro identificador de aplicación y carpetas temporales:

```sh
export HILO_APP_ID=dev.hilo.Prueba
export XDG_STATE_HOME="$(mktemp -d)"
export XDG_CONFIG_HOME="$(mktemp -d)"
./hilo.py
```

- `HILO_APP_ID` hace que la copia de desarrollo sea una aplicación aparte.
- `XDG_STATE_HOME` aísla la sesión, el texto guardado, el historial de Claude y los paneles cerrados.
- `XDG_CONFIG_HOME` aísla las preferencias y los atajos.

Si quiere conservar la sesión de prueba entre arranques (por ejemplo, para probar la restauración), use una carpeta fija en lugar de `mktemp -d`.

`./hilo.py --fresh` abre sin restaurar nada. Con la copia de desarrollo abierta, `./hilo.py --actualizar` (con las mismas variables) aplica sus cambios sin cerrarla.

Para probar sin que aparezca ninguna ventana en su escritorio, use una pantalla virtual: `Xvfb :97 &` y luego `DISPLAY=:97 GDK_BACKEND=x11 ./hilo.py`.

## Cómo está organizado

Todo vive en `hilo.py`, a propósito: se instala con un enlace y no hay paquetes que mantener.

| Pieza | Qué hace |
|---|---|
| Funciones sueltas al inicio | Configuración, escritura atómica, lectura de `/proc` (comando, carpeta, consumo) y todo lo de Claude Code (encontrar la sesión, armar el `--resume`). |
| `Pane` | Una terminal. Arranca la shell, restaura el texto guardado, detecta qué está corriendo y sabe describirse para guardarse. |
| `Tab` | Una pestaña. Guarda un árbol de paneles (`Gtk.Paned` anidados): dividir, cerrar, mover el foco, maximizar y recibir pestañas arrastradas. |
| `TermWindow` | Una ventana con su barra de pestañas, la búsqueda, el menú contextual y los atajos. |
| `PreferencesWindow` | Preferencias: opciones generales y la captura de atajos. |
| `HelpWindow` | La ventana de atajos y controles: los atajos vigentes, las teclas fijas y los gestos con el mouse. |
| `HiloApp` | La aplicación. Guarda la sesión cada pocos segundos, la restaura al abrir, mide el consumo, lleva el historial de Claude y hace el guardado final al apagar. |

### Formato de la sesión

`session.json` tiene una lista de ventanas, y cada ventana una lista de pestañas. El contenido de cada pestaña es un árbol:

```json
{ "split": "h", "ratio": 0.5, "children": [
    { "pane": { "id": "…", "cwd": "/ruta", "command": { "argv": ["nvim"] }, "claude_ids": [] } },
    { "pane": { "id": "…", "cwd": "/ruta", "command": null, "claude_ids": [] } }
] }
```

Si cambia este formato, asegúrese de que una sesión guardada con la versión anterior se siga abriendo. Alguien puede actualizar Hilo justo después de un apagón.

### Reglas que no hay que romper

- **Toda escritura a disco pasa por `atomic_write`.** Un apagón a mitad de una escritura no puede dejar un archivo corrupto.
- **Nada peligroso se ejecuta solo.** Al restaurar, solo Claude y la lista `autorun` se vuelven a lanzar. Cualquier otro comando queda escrito en el prompt esperando Enter.
- **El guardado final al apagar no se puede perder.** Si toca el manejo de señales (`SIGTERM`, `SIGHUP`) o el cierre de procesos hijos, pruebe que la sesión siga completa después de un apagado.
- **La actualización en caliente tiene que seguir funcionando.** `hot_reload` le pasa el estado vivo (incluido el `relevo` de cada panel: el fd del pty, el pid de la shell y el texto en pantalla) al código nuevo con `exec`. La versión nueva tiene que aceptar lo que serializó la anterior.
- **Liviana.** Hilo vigila los procesos cada pocos segundos. Nada de trabajo pesado en esos temporizadores.

## Probar un cambio

No hay suite automática todavía. Antes de proponer un cambio, revise a mano lo que tocó, siempre con la copia de desarrollo:

- **Restauración:** abra pestañas y paneles en carpetas distintas, con algo corriendo, y mate el proceso con `kill -9`. Al abrir de nuevo, todo debe volver igual.
- **Apagado limpio:** ciérrela con `kill -TERM`. La sesión debe quedar guardada con los comandos que estaban corriendo.
- **Salir a propósito:** escriba `exit` en todas las pestañas. Al abrir de nuevo no debe restaurarse nada.
- **Paneles y arrastre:** divida, cierre, maximice y arrastre pestañas dentro de la ventana, a otra ventana y fuera de toda ventana.
- **Atajos:** cambie un atajo en Preferencias, reinicie y compruebe que se mantiene.

Como mínimo, el archivo tiene que compilar:

```sh
python3 -m py_compile hilo.py
```

## Convenciones

- La interfaz viene en español y en inglés. En el código cada texto se escribe en español dentro de `T("…")`, y su versión en inglés va en el diccionario `EN` al comienzo de `hilo.py`. Un texto nuevo sin su entrada en inglés le aparece en español a quien usa inglés. Ambos en lenguaje natural; el español, tratando de usted.
- La documentación tiene dos versiones: inglés (`README.md`, `CONTRIBUTING.md`) y español (`README.es.md`, `CONTRIBUTING.es.md`). Si cambia una, actualice la otra.
- Código claro antes que ingenioso. Los comentarios explican el porqué, no el qué.
- No agregue dependencias fuera de Python, GTK y VTE sin conversarlo antes.
- Un cambio por propuesta, con un mensaje de commit que diga qué cambia y para qué.

## Proponer un cambio

1. Haga un fork del repositorio y cree una rama desde `main`.
2. Haga el cambio y pruébelo como se describe arriba.
3. Abra un pull request explicando qué cambió, por qué y cómo lo probó.

`main` está protegida: los cambios solo entran por pull request, con la aprobación del mantenedor.

Al contribuir, usted acepta que su aporte se publique bajo la licencia MIT del proyecto.

Si encuentra un error, abra un issue con los pasos para reproducirlo y, si aplica, el contenido de `session.json` sin rutas ni datos personales.
