# Sea-Bird · conversión batch sin GUI

`DatCnvW.exe` **no** elige solas las variables de salida: esas opciones están en un **Program setup** (`.psa`). Sin un `.psa` válido, Windows suele abrir el módulo gráfico y pedir directorios y variables.

## Qué hacer una sola vez (por laboratorio / campaña)

1. En **SBE Data Processing**, abre **DatCnv**.
2. Configura **variables de salida** y el resto como quieras para tus CTD.
3. **File → Save Program Setup As…** y guarda como `DatCnv.psa`.
4. Copia ese fichero a **este directorio** del repo con el nombre exacto:

   `tools/seabird/DatCnv.psa`

   Así el comando `python run/ieo_cli.py convert-cnv --project-root .` lo encontrará en cualquier máquina que tenga el repo (servidores incluidos).

## Otros servidores (sin copiar el .psa al repo)

Define rutas absolutas en el entorno del proceso:

| Variable | Uso |
|----------|-----|
| `IEO_SEABIRD_DATCNVW` | Ruta a `DatCnvW.exe` si no está en `Program Files (x86)\Sea-Bird\...` |
| `IEO_SEABIRD_DATCNV_PSA` | Ruta a tu `DatCnv.psa` (misma configuración de variables en todos los lotes) |

## ¿Puede el script deducir las variables solo del `.hex` / `data/raw`?

No de forma fiable portable: el **`.xmlcon`** describe canales del instrumento, pero el **`.psa`** fija qué columnas escribe el CNV y opciones del módulo. Lo habitual es **un `.psa` por política de procesado** (p. ej. “todas las derivadas estándar”) y reutilizarlo.

Si cambias de versión mayor de Sea-Bird, revisa que el `.psa` siga siendo compatible.
