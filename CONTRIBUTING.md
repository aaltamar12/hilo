# Cómo contribuir

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

`./hilo.py --fresh` abre sin restaurar nada.

## Cómo está organizado

Todo vive en `hilo.py`, a propósito: se instala con un enlace y no hay paquetes que mantener.

| Pieza | Qué hace |
|---|---|
| Funciones sueltas al inicio | Configuración, escritura atómica, lectura de `/proc` (comando, carpeta, consumo) y todo lo de Claude Code (encontrar la sesión, armar el `--resume`). |
| `Pane` | Una terminal. Arranca la shell, restaura el texto guardado, detecta qué está corriendo y sabe describirse para guardarse. |
| `Tab` | Una pestaña. Guarda un árbol de paneles (`Gtk.Paned` anidados): dividir, cerrar, mover el foco, maximizar y recibir pestañas arrastradas. |
| `TermWindow` | Una ventana con su barra de pestañas, la búsqueda, el menú contextual y los atajos. |
| `PreferencesWindow` | Preferencias: opciones generales y la captura de atajos. |
| `HiloApp` | La aplicación. Guarda la sesión cada pocos segundos, la restaura al abrir, mide el consumo, lleva el historial de Claude y hace el guardado final al apagar. |

### Formato de la sesión

`session.json` tiene una lista de ventanas, y cada ventana una lista de pestañas. El contenido de cada pestaña es un árbol:

```json
{ "split": "h", "ratio": 0.5, "children": [
    { "pane": { "id": "…", "cwd": "/ruta", "command": ["nvim"] } },
    { "pane": { "id": "…", "cwd": "/ruta", "command": null } }
] }
```

Si cambia este formato, asegúrese de que una sesión guardada con la versión anterior se siga abriendo. Alguien puede actualizar Hilo justo después de un apagón.

### Reglas que no hay que romper

- **Toda escritura a disco pasa por `atomic_write`.** Un apagón a mitad de una escritura no puede dejar un archivo corrupto.
- **Nada peligroso se ejecuta solo.** Al restaurar, solo Claude y la lista `autorun` se vuelven a lanzar. Cualquier otro comando queda escrito en el prompt esperando Enter.
- **El guardado final al apagar no se puede perder.** Si toca el manejo de señales (`SIGTERM`, `SIGHUP`) o el cierre de procesos hijos, pruebe que la sesión siga completa después de un apagado.
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

- Los textos de la interfaz, los mensajes y el README van en español, en lenguaje natural y tratando de usted.
- Código claro antes que ingenioso. Los comentarios explican el porqué, no el qué.
- No agregue dependencias fuera de Python, GTK y VTE sin conversarlo antes.
- Un cambio por propuesta, con un mensaje de commit que diga qué cambia y para qué.

## Proponer un cambio

1. Cree una rama desde `main`.
2. Haga el cambio y pruébelo como se describe arriba.
3. Abra un pull request explicando qué cambió, por qué y cómo lo probó.

Si encuentra un error, abra un issue con los pasos para reproducirlo y, si aplica, el contenido de `session.json` sin rutas ni datos personales.
