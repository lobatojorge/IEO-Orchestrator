## Carpeta `run/` — Ejecución del proyecto

Esta carpeta agrupa **todos los artefactos necesarios para ejecutar** el repositorio sin ruido en la raíz.

- `app.py`: entrada de la aplicación **Streamlit** (visor interactivo).
- `main.py`: **orquestador** del pipeline IEO (lee `data/cnv/` → Parquet e informes en `outputs/runs/<run_id>/`).
- `ieo_cli.py`: CLI del paquete `ieo` (p. ej. `convert-cnv` sin configurar `PYTHONPATH`).
- `lanzador.bat`: script de conveniencia para Windows que:
  - crea/activa un entorno virtual `venv/`,
  - instala `requirements.txt`,
  - lanza `streamlit run app.py` (no ejecuta `main.py`; hazlo antes si quieres el pipeline IEO).
- `requirements.txt`: lista de dependencias Python del proyecto.
- `.gitignore`: reglas de exclusión aplicadas a esta carpeta (útil si se generan artefactos locales aquí).
- `assets/`: recursos estáticos necesarios para la UI (por ejemplo, `logo.webp` usado como icono de la app).

### Uso rápido

Desde la raíz del repo:

```bash
cd run
python -m venv venv         # si no existe
venv\Scripts\activate       # Windows
pip install -r requirements.txt
python main.py              # Pipeline IEO: lee data/cnv/ → outputs/runs/… (Parquet + reportes)
streamlit run app.py        # Visor web (Streamlit)
```

Equivalente sin entrar en `run/` (desde la raíz del repositorio):

```bash
python run/main.py
streamlit run run/app.py
```

### De `data/cnv` al Streamlit (pestaña **Radiales Gijón**)

1. Coloca uno o más **`.cnv`** en **`data/cnv/`** (p. ej. tras `python run/ieo_cli.py convert-cnv --project-root .`, véase `tools/seabird/README.md`).
2. **`streamlit run app.py`**: el visor **lee esos CNV** y calcula la temperatura mensual a **5 m** + figura **Marcos + ATAC** por estación.
3. **Estación**: si el CNV no trae columna `estacion`, se infiere del **nombre del fichero** (p. ej. `…E2…` → estación 2). Ajusta los nombres si hace falta.
4. **Tiempo**: debe existir una columna de tiempo reconocible tras la normalización, o la cabecera típica Sea-Bird **`# start_time = …`** para fijar la marca temporal del lance.
5. **Opcional** — **`python main.py`**: pipeline IEO (Parquet + informes en `outputs/runs/…`); es independiente del visor.
6. **Compatibilidad**: si **no** hay ningún `.cnv` en `data/cnv/`, el visor intenta el CSV legado **`data/processed/sireno_gijon_ctd_processed.csv`**.

### Dónde poner los datos

- **Visor + pipeline IEO (entrada común)**: `data/cnv/` (ficheros `.cnv`)
- **Visor solo (fallback)**: `data/processed/sireno_gijon_ctd_processed.csv`
- **Backup CTD**: `data/raw/` (`.hex/.xmlcon/.hdr/.bl`)

