"""
DEPRECADO: `src/01_agent_inspector.py`
=====================================

Este módulo existía en versión Pandas. En producción el pipeline ya integra:
- detección de anomalías (Isolation Forest) sin Pandas
- registro inmutable (audit_log) por corrida
- reportes HTML + JSON por checkpoint

Uso recomendado (práctico)
--------------------------
Desde `run/`:
    python main.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    src_dir = project_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from ieo.cli import run_pipeline  # noqa: E402

    raise SystemExit(int(run_pipeline(project_root=project_root)))


if __name__ == "__main__":
    main()
