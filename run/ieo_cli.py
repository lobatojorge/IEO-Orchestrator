"""
CLI del pipeline IEO sin configurar PYTHONPATH.

Desde la raíz del repositorio:

    python run/ieo_cli.py run --project-root .

Equivale a `python -m ieo run` con `src` en el path de importación.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RUN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _RUN_DIR.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ieo.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
