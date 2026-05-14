## Carpeta `run/` — Ejecución

- `app.py`: barra lateral con **Ejecutar pipeline ahora** (subproceso `python run/main.py` desde la raíz del repo).
- `pipeline_runs.py`: utilidades puras (listar corridas, cargar Parquet, provenance); usadas por `app.py` y por `pytest`.
- `main.py`: orquestador del **pipeline** (`src/ieo`).
- `ieo_cli.py`: `python run/ieo_cli.py run --project-root .` (equivale a `python -m ieo run` con `src` en el path).
- `lanzador.bat`: crea/activa `venv/`, instala dependencias y lanza Streamlit (no ejecuta `main.py`).
- `requirements.txt`: dependencias.
- `assets/`: p. ej. `logo.webp` para la app.

### Datos

1. **Entrada del pipeline:** `data/processed/perfiles_all.csv` (el visor **no** lee este CSV; lee Parquet generados).
2. Si el histórico largo está en **`data/raw/perfiles_all.csv`** (columnas `Fecha`, `Est`, `Press`, `Temp`, `Sal`, …), materializa el processed con:  
   `python run/build_processed_from_raw.py`  
   (desde la raíz del repo o con `--project-root`).
3. Tras `python run/main.py`, el visor muestra en la barra lateral el selector de corrida y, si existe, **Provenance** (`provenance.json`). En la página principal, el expander **«Fuente de datos (corrida seleccionada)»** resume filas y rutas.
4. **`data/raw/`**: crudos; el pipeline no lo lee directamente hasta ejecutar el script anterior.

### Comandos

```bash
cd run
pip install -r requirements.txt
python main.py
streamlit run app.py
```

El pipeline escribe bajo `outputs/runs/<run_id>/` (Parquet, checkpoints HTML, bitácora). Esa salida no se versiona por defecto (`.gitignore`).

### Docker y TFM

Ver [`../docs/operacion_tfm.md`](../docs/operacion_tfm.md) y [`../README.md`](../README.md).
