"""
Prueba de detección de anomalías zero-shot con TimeGPT (Nixtla).

Requisitos:
    pip install nixtla pandas

Uso:
    # Windows PowerShell:
    $env:NIXTLA_API_KEY = "tu_clave"
    python src/timegpt.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_and_prepare_timegpt_frame(csv_path: Path) -> pd.DataFrame:
    """
    Carga el CSV procesado y devuelve un DataFrame con columnas estrictas:
    ``ds`` (timestamp), ``y`` (temperatura), ``unique_id`` (estación).

    Si hay varias mediciones el mismo día para una estación, se promedia ``y``
    (TimeGPT espera típicamente un valor por instante y serie).
    """
    df = pd.read_csv(csv_path, sep=None, engine="python")
    df.columns = [str(c).lower().strip() for c in df.columns]

    col_fecha = next((c for c in df.columns if c in ("fecha", "fecha_muestreo", "date")), None)
    col_temp = next((c for c in df.columns if "temp" in c), None)
    col_est = next((c for c in df.columns if c == "estacion"), None)

    if col_fecha is None or col_temp is None:
        raise ValueError("No se encontraron columnas de fecha y/o temperatura en el CSV.")
    if col_est is None:
        df["unique_id"] = "global"
    else:
        df["unique_id"] = df[col_est].astype(str)

    df["ds"] = pd.to_datetime(df[col_fecha], errors="coerce").dt.floor("D")
    df["y"] = pd.to_numeric(df[col_temp], errors="coerce")
    work = df.dropna(subset=["ds", "y"])[["unique_id", "ds", "y"]]

    out = work.groupby(["unique_id", "ds"], as_index=False)["y"].mean()
    out = out.sort_values(["unique_id", "ds"]).reset_index(drop=True)
    return out


def run_anomaly_detection(
    df: pd.DataFrame,
    *,
    freq: str = "D",
    level: int = 95,
) -> pd.DataFrame:
    """
    Llama a TimeGPT vía NixtlaClient.detect_anomalies.

    Parameters
    ----------
    df
        Debe contener ``unique_id``, ``ds``, ``y``.
    freq
        Frecuencia de la serie (p. ej. ``'D'`` diaria tras ``dt.floor('D')``).
    level
        Nivel de intervalo de confianza (0–100), p. ej. 95 para 95 %.
    """
    # -------------------------------------------------------------------------
    # API key Nixtla / TimeGPT:
    #   - Recomendado: exportar NIXTLA_API_KEY (no commitear secretos).
    #   - Solo local: tras leer env, descomenta y usa la segunda línea con tu clave.
    # -------------------------------------------------------------------------
    api_key = os.environ.get("NIXTLA_API_KEY", "").strip()
    # api_key = api_key or "PEGA_AQUI_TU_API_KEY"
    if not api_key:
        raise RuntimeError(
            "Configura la API key: variable NIXTLA_API_KEY o descomenta la línea "
            "'api_key = api_key or ...' en run_anomaly_detection (solo pruebas locales)."
        )

    from nixtla import NixtlaClient

    client = NixtlaClient(api_key=api_key)
    result = client.detect_anomalies(
        df,
        freq=freq,
        id_col="unique_id",
        time_col="ds",
        target_col="y",
        level=level,
    )
    return result


def print_first_anomalies(result: pd.DataFrame, n: int = 10) -> None:
    """Imprime las primeras ``n`` filas marcadas como anomalía por el modelo."""
    flag = None
    for name in ("anomaly", "is_anomaly"):
        if name in result.columns:
            flag = name
            break
    if flag is None:
        print("No se encontró columna booleana de anomalías en la salida.")
        print("Columnas disponibles:", list(result.columns))
        return

    flagged = result[result[flag] == True].copy()  # noqa: E712
    if flagged.empty:
        print("No se detectaron anomalías según la columna", repr(flag))
        return
    print(flagged.head(n).to_string())


def main() -> None:
    csv_path = _project_root() / "data" / "processed" / "sireno_gijon_ctd_processed.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No existe el archivo: {csv_path}")

    prepared = load_and_prepare_timegpt_frame(csv_path)
    if prepared.empty:
        raise RuntimeError("No quedaron filas válidas tras preparar ds / y / unique_id.")

    out = run_anomaly_detection(prepared, freq="D", level=95)
    print_first_anomalies(out, n=10)


if __name__ == "__main__":
    main()
