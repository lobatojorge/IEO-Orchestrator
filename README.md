# IEO Orchestrator — Pipeline y Visualización de Series Temporales Oceanográficas

> **Trabajo de Fin de Máster** | Pipeline de datos y plataforma web interactiva para el análisis de radiales oceanográficos del IEO (Instituto Español de Oceanografía).

---

## ¿Qué hace este proyecto?

Diseña e implementa un **pipeline integral de datos** (ingesta, validación de calidad, análisis estadístico) y una **plataforma web interactiva** (Streamlit) para explorar series temporales históricas de muestreos marinos.

La arquitectura prioriza **automatización**, **reproducibilidad**, **control de calidad riguroso del dato** y su accesibilidad a través de visualizaciones interactivas.

---

## 🏗️ Estructura del Proyecto

```text
IEO-Orchestrator/
├── data/                      ← Ciclo de vida de los datos (ver nota ↓)
│   ├── raw/                   Datos brutos originales (punto de partida)
│   ├── interim/               Archivos temporales generados al unificar formatos
│   ├── processed/             Datos finales limpios y validados
│   ├── quarantine/            Registros marcados por el inspector ML
│   └── pending_review/        Datos pendientes de revisión manual
├── src/                       ← Lógica de backend y análisis
│   ├── 00_data_scout.py       Reconocimiento y reporte de datos en raw/
│   ├── 00_ingestion.py        Estandarización y limpieza estructural
│   ├── 01_agent_inspector.py  Auditoría de calidad y observabilidad
│   ├── 02_analysis.py         Cálculos, estadística y modelado
│   ├── 03_visualization.py    Gráficos Plotly y utilidades de visualización
│   ├── visualization.py       Fachada de importación para `run/app.py`
│   └── fig_export.py          Exportación de figuras a PNG
├── outputs/                   ← Productos generados por los scripts
│   ├── figures/               Gráficos generados (PNG)
│   └── reports/               Informes de validación de calidad
├── docs/                      ← Documentación y contenido editorial
│   ├── metodologia_*.md       Textos metodológicos (cargados por la web)
│   ├── data_dictionary.yaml   Diccionario de variables procesadas
│   └── descripcion_Radiales.txt
├── run/                       ← Artefactos de ejecución (CLI + web)
│   ├── app.py                 Frontend (Streamlit) — plataforma web interactiva
│   ├── main.py                Orquestador — ejecuta el pipeline completo
│   ├── lanzador.bat           Script de arranque rápido para Windows
│   ├── requirements.txt       Dependencias Python
│   ├── assets/                Logo y recursos de la interfaz web
│   └── README.md              Guía rápida de uso de `run/`
└── README.md
```

> [!IMPORTANT]
> Por diseño, el repositorio **no versiona datos reales** (carpetas `data/` y `outputs/` se ignoran en git salvo `.gitkeep`).
> En la rama **main**, la ingesta activa es **ficheros Sea-Bird `.cnv`** en `data/raw/` (patrón de nombre documentado en `src/00_ingestion.py`). El flujo **Excel** puede vivir en otra rama; la función `ingest_sireno_ctd()` sigue en el módulo para ese caso.

---

## ⚙️ Flujo de Trabajo

```
data/raw/  →  00_data_scout.py  →  00_ingestion.py  →  data/interim + data/processed
                                        ↓
                              01_agent_inspector.py  →  data/processed + data/quarantine/
                                        ↓
                              02_analysis.py  →  data/processed/sireno_gijon_ctd_forecast.csv (opcional)
                                        ↓
                              run/app.py (Streamlit)
```

1. **Reconocimiento** (`00_data_scout.py`): genera un reporte de la estructura del raw antes de procesar nada.
2. **Ingesta** (`00_ingestion.py`): en **main**, lee `.cnv` y escribe el mismo maestro en **`processed`** e **`interim`** (`sireno_gijon_ctd_*.csv`) para que el inspector tenga entrada.
3. **Inspección** (`01_agent_inspector.py`): lee `interim`, audita con Isolation Forest (o fallback); salida saneada a `processed/` y sospechosos a `quarantine/`.
4. **Análisis** (`02_analysis.py`): pronóstico mensual SARIMAX + export `sireno_gijon_ctd_forecast.csv` (la UI Streamlit puede usar solo el CSV processed; el forecast es producto batch aparte).
5. **Visualización** (`03_visualization.py` vía `visualization.py`): figuras Plotly reutilizables por la app.
6. **Plataforma web** (`run/app.py`): exploración interactiva.

---

## 🚀 Instalación y Arranque

### 1. Entorno Python

```bash
cd run
# Python 3.11 o 3.12 recomendado
pip install -r requirements.txt
```

### 2. Datos

En **main**, deposita los **`.cnv`** en `data/raw/` (ver regex de nombre en `ingest_cnv` dentro de `src/00_ingestion.py`).
> ⚠️ Los datos reales del IEO **no se incluyen** en este repositorio.

### 3. Ejecutar el pipeline

```bash
cd run
python main.py
```

### 4. Lanzar la plataforma web

```bash
cd run
streamlit run app.py
```

En Windows puedes usar `run/lanzador.bat` para arrancar directamente.

---

## 🛠️ Stack Tecnológico

| Capa | Tecnologías |
|---|---|
| Orquestación e ingesta | Python, Pandas, Pathlib, **ctd** (lectura `.cnv`) |
| Calidad de datos | Isolation Forest (scikit-learn) u opción estadística si sklearn no está |
| Series temporales (módulo 02) | **statsmodels** (SARIMAX); instalar con `pip install -r requirements.txt` |
| Visualización | Plotly (app), Seaborn/Matplotlib (scripts) |
| Plataforma web | Streamlit |

---

## 📁 Notas

- Los textos metodológicos se editan en `docs/metodologia_*.md` y la web los carga automáticamente.
- `src/archive/` contiene módulos en desarrollo no activos en el pipeline principal. No se incluye en el repositorio.
- La carpeta `auditoria_advicExplorer/` (análisis de la aplicación R Shiny adviceXplorer del ICES) se mantiene localmente y no forma parte de este repositorio.
