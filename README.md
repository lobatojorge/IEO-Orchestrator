# IEO Orchestrator — Pipeline y Visualización de Series Temporales Oceanográficas


## ¿Qué hace este proyecto?

Este primer boceto del MVP trabaja sobre archivos.xsl. Tras entender que el CTD genera archivos propios, pensé que sería buena idea trabajar sobre ellos y, de esta forma, minimizar el sesgo humano

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
│   ├── 03_visualization.py    Generación de gráficos estáticos
│   ├── visualization.py       Fachada de importación para app.py
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
