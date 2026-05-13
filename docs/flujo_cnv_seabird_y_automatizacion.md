# Flujo operativo: Sea-Bird → `.cnv` → pipeline

## Objetivo

Minimizar fricción en Windows y entornos corporativos: el pipeline **no ejecuta** herramientas Sea-Bird.
El técnico convierte los ficheros del CTD a **`.cnv`** con el procedimiento habitual del laboratorio y vuelca los `.cnv` en `data/cnv/`.

## Decisión de diseño (2026-05)

- **Entrada del pipeline**: `data/cnv/` (ficheros procesables: `.cnv` y opcionalmente `.xls/.xlsx` / `.nc`)
- **Copia de seguridad**: `data/raw/` (ficheros crudos del CTD: `.hex`, `.xmlcon`, `.hdr`, `.bl`)

Motivo: integrar Sea-Bird “en caliente” (ejecutando `DatCnvW.exe`) puede fallar por permisos/UAC/AppLocker/Defender y rompe la automatización.

## Flujo recomendado (sin fricción)

1. Copiar del CTD a `data/raw/` todos los ficheros crudos del cast:
   - `<CAST>.hex`
   - `<CAST>.xmlcon`
   - opcional: `<CAST>.hdr`, `<CAST>.bl`
2. Convertir a `.cnv` con Sea-Bird SBE Data Processing (GUI).
3. Copiar el resultado a `data/cnv/`:
   - `<CAST>.cnv`
4. Ejecutar el pipeline:
   - `python run/main.py`

## Opciones de automatización (para retomar en el futuro)

### Opción A — Script auxiliar (recomendado si se automatiza)

Mantener el pipeline “puro” (solo lee `.cnv`) y añadir un script aparte que intente convertir:

- `tools/convert_seabird.ps1` (o `.bat`) que:
  - detecte conjuntos `.hex + .xmlcon` en `data/raw/`
  - invoque `DatCnvW.exe` para generar `.cnv` en `data/cnv/`
  - escriba un log en `outputs/runs/<run_id>/checkpoints/00_convert_cnv.*`

Ventajas:
- Si hay bloqueo de permisos, **falla el script** pero el pipeline sigue siendo estable.
- Evita mezclar problemas de entorno con errores de datos.

### Opción B — Integración dentro del pipeline (no recomendada por defecto)

Antes de detectar fuentes, el pipeline podría:
- descubrir casts en `data/raw/` (`.hex + .xmlcon`)
- convertir a `data/cnv/`
- continuar con el flujo normal

Riesgos:
- `PermissionError: [WinError 5] Acceso denegado` (UAC/AppLocker/Defender)
- rutas variables de instalación
- ejecución silenciosa colgada (procesos que no devuelven output/exit code)

### Opción C — VM/Contenedor “preparado”

Para entornos corporativos con bloqueos:
- ejecutar conversión + pipeline en una VM donde Sea-Bird esté instalado y permitido

Trade-off: más coste operativo, pero máxima reproducibilidad del entorno.

## Nota sobre la CLI del repo

Este repo incluye un comando opcional (no requerido por el flujo recomendado):

- `python run/ieo_cli.py convert-cnv --project-root .` (desde la raíz del repo; no requiere `PYTHONPATH`).
- Alternativa: `python -m ieo convert-cnv --project-root .` con `src` en `PYTHONPATH` (p. ej. `$env:PYTHONPATH="src"` en PowerShell).

Lee casts de `data/raw/` y escribe `.cnv` en `data/cnv/`, invocando **DatCnvW** en modo batch con **`/p` apuntando a un `DatCnv.psa`**. Sin ese fichero, Sea-Bird suele abrir la GUI y pedir directorios y variables.

### Sin GUI (batch)

1. Una vez en **DatCnv** (SBE Data Processing), elige variables de salida y **File → Save Program Setup As…**.
2. Copia el `.psa` a `tools/seabird/DatCnv.psa` en este repo (véase `tools/seabird/README.md`) **o** define en el servidor:
   - `IEO_SEABIRD_DATCNV_PSA` — ruta absoluta al `.psa`
   - `IEO_SEABIRD_DATCNVW` — ruta absoluta a `DatCnvW.exe` si no está en `Program Files (x86)\Sea-Bird\...`

Opcional: `--psa-path "C:\ruta\DatCnv.psa"`.

El **`.xmlcon`** no sustituye al `.psa`: describe el instrumento; el **`.psa`** fija qué columnas escribe el CNV.

