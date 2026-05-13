from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_SKIP_DIR_PARTS = frozenset({".git", "__pycache__"})


@dataclass(frozen=True, slots=True)
class SeabirdCastSet:
    """
    Conjunto de ficheros crudos Sea-Bird que normalmente describen un cast.

    Mínimo para convertir:
    - .hex (datos crudos)
    - .xmlcon (configuración instrumental para decodificar)
    """

    stem: str
    hex_path: Path
    xmlcon_path: Path
    hdr_path: Path | None = None
    bl_path: Path | None = None

    def guess_timestamp_text(self) -> str | None:
        """
        Extrae algo tipo "System UTC = Apr 02 2026 09:49:57" si existe.
        No garantiza fecha de estación; es un mínimo útil para trazabilidad.
        """

        for p in (self.hdr_path, self.hex_path):
            if p is None or not p.exists():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            m = re.search(r"^\*\s*System\s+UTC\s*=\s*(.+?)\s*$", txt, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip()
        return None


def _optional_sidecar(parent: Path, stem: str, suffix_lower: str) -> Path | None:
    for f in parent.iterdir():
        if not f.is_file():
            continue
        if f.stem != stem or f.suffix.lower() != suffix_lower:
            continue
        return f
    return None


def discover_cast_sets(raw_dir: Path, *, max_depth: int = 24) -> list[SeabirdCastSet]:
    """
    Encuentra casts Sea-Bird: cada ``.hex`` con un ``.xmlcon`` (mismo nombre base / stem)
    en **el mismo directorio** que el ``.hex``.

    - Recorre ``raw_dir`` en profundidad (``rglob('*.hex')``), con límite de niveles.
    - Ignora rutas bajo ``.git``, ``__pycache__`` o carpetas cuyo nombre empiece por ``.``.
    - Empareja ``.xmlcon`` / ``.XMLCON`` indistintamente.
    """

    if not raw_dir.exists():
        return []

    raw_dir = raw_dir.resolve()
    out: list[SeabirdCastSet] = []

    for hex_path in sorted(raw_dir.rglob("*.hex")):
        if not hex_path.is_file():
            continue
        try:
            rel = hex_path.relative_to(raw_dir)
        except ValueError:
            continue
        if any(p.startswith(".") for p in rel.parts):
            continue
        if any(p in _SKIP_DIR_PARTS for p in rel.parts):
            continue
        if len(rel.parts) > max_depth:
            continue

        parent = hex_path.parent
        stem = hex_path.stem
        xmlcon_path = _optional_sidecar(parent, stem, ".xmlcon")
        if xmlcon_path is None:
            continue

        hdr_path = _optional_sidecar(parent, stem, ".hdr")
        bl_path = _optional_sidecar(parent, stem, ".bl")

        out.append(
            SeabirdCastSet(
                stem=stem,
                hex_path=hex_path,
                xmlcon_path=xmlcon_path,
                hdr_path=hdr_path,
                bl_path=bl_path,
            )
        )

    return out


def _guess_datcnv_program_setup(exe_dir: Path) -> Path | None:
    """Localiza un ``.psa`` de DatCnv junto a ``DatCnvW.exe``."""

    if not exe_dir.is_dir():
        return None
    for p in sorted(exe_dir.glob("*.psa")):
        name = p.name.lower()
        if "datcnv" in name or "dat_cnv" in name:
            return p
    return None


def resolve_datcnvw_exe() -> Path:
    """
    Localiza ``DatCnvW.exe``.

    Orden: ``IEO_SEABIRD_DATCNVW``, rutas típicas bajo ``Program Files``.
    """

    env = (os.environ.get("IEO_SEABIRD_DATCNVW") or "").strip().strip('"')
    if env:
        p = Path(env)
        if p.is_file():
            return p.resolve()
        raise FileNotFoundError(f"IEO_SEABIRD_DATCNVW no apunta a un ejecutable válido: {env}")

    candidates = [
        Path(r"C:\Program Files (x86)\Sea-Bird\SBEDataProcessing-Win32\DatCnvW.exe"),
        Path(r"C:\Program Files\Sea-Bird\SBEDataProcessing-Win32\DatCnvW.exe"),
    ]
    for p in candidates:
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        "No se encontró DatCnvW.exe. Instala Sea-Bird SBE Data Processing o define IEO_SEABIRD_DATCNVW."
    )


def resolve_datcnv_psa(*, project_root: Path, exe: Path, psa_override: Path | None = None) -> Path | None:
    """
    Localiza ``DatCnv.psa`` (configuración de variables y modo batch).

    Orden: ``psa_override``, ``IEO_SEABIRD_DATCNV_PSA``, ``tools/seabird/DatCnv.psa``
    en el repo, luego ``*.psa`` junto a ``DatCnvW.exe``.
    """

    if psa_override is not None:
        p = psa_override.resolve()
        return p if p.is_file() else None

    env = (os.environ.get("IEO_SEABIRD_DATCNV_PSA") or "").strip().strip('"')
    if env:
        p = Path(env)
        if p.is_file():
            return p.resolve()

    bundled = (project_root / "tools" / "seabird" / "DatCnv.psa").resolve()
    if bundled.is_file():
        return bundled

    return _guess_datcnv_program_setup(exe.parent)


def build_datcnvw_command_template(
    project_root: Path,
    *,
    psa_override: Path | None = None,
) -> str:
    """
    Plantilla de línea de órdenes para DatCnv en modo batch (``/p`` + ``/s``).

    Sin ``.psa`` no hay conversión reproducible sin GUI: se lanza ``ValueError``
    con instrucciones (copiar PSA al repo o usar variables de entorno).
    """

    exe = resolve_datcnvw_exe()
    psa = resolve_datcnv_psa(project_root=project_root, exe=exe, psa_override=psa_override)
    if psa is None or not psa.is_file():
        raise ValueError(
            "Falta DatCnv.psa (program setup): sin él Sea-Bird suele abrir la GUI.\n"
            "· Copia el fichero guardado desde DatCnv a: tools/seabird/DatCnv.psa\n"
            "  (véase tools/seabird/README.md), o\n"
            "· define IEO_SEABIRD_DATCNV_PSA con la ruta absoluta al .psa, o\n"
            "· instala Sea-Bird y deja un *DatCnv*.psa junto a DatCnvW.exe.\n"
            "Generación del .psa (una vez): DatCnv → elige variables → File → Save Program Setup As…"
        )

    return (
        f'"{exe}" /p "{psa.resolve()}" /i "{{hex}}" /c "{{xmlcon}}" '
        f'/o "{{out_dir}}" /f "{{out_name}}" /s'
    )


def xmlcon_sensor_hints(xmlcon_path: Path, *, limit: int = 24) -> list[str]:
    """
    Nombres de sensor declarados en el ``.xmlcon`` (solo informativo; no sustituye al ``.psa``).
    """

    try:
        txt = xmlcon_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = re.findall(r'\bSensor\s*=\s*"([^"]+)"', txt, flags=re.IGNORECASE)
    out: list[str] = []
    for s in found:
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def build_command_from_template(*, template: str, cast: SeabirdCastSet, out_cnv: Path) -> list[str]:
    """
    Construye un comando a ejecutar a partir de una plantilla.

    La plantilla admite placeholders:
    - {hex}     ruta absoluta al .hex
    - {xmlcon}  ruta absoluta al .xmlcon
    - {hex_name}   solo nombre de fichero del .hex (útil con cwd en su carpeta)
    - {xmlcon_name} solo nombre del .xmlcon
    - {hdr}     ruta al .hdr (o vacío si no existe)
    - {bl}      ruta al .bl (o vacío si no existe)
    - {out}     ruta completa de salida .cnv
    - {out_dir} directorio de salida (padre de {out})
    - {out_name} nombre de fichero de salida (ej. "SHE_001.cnv")
    - {stem}    nombre base del cast (ej. "SHE_001")

    Se parsea con shlex (Windows-friendly si la plantilla está bien entrecomillada).
    """

    out_parent = out_cnv.parent.resolve()
    out_dir_arg = str(out_parent)
    if sys.platform == "win32" and not out_dir_arg.endswith(("\\", "/")):
        out_dir_arg += "\\"

    s = template.format(
        hex=str(cast.hex_path.resolve()),
        xmlcon=str(cast.xmlcon_path.resolve()),
        hex_name=cast.hex_path.name,
        xmlcon_name=cast.xmlcon_path.name,
        hdr=str(cast.hdr_path.resolve()) if cast.hdr_path else "",
        bl=str(cast.bl_path.resolve()) if cast.bl_path else "",
        out=str(out_cnv.resolve()),
        out_dir=out_dir_arg,
        out_name=str(out_cnv.name),
        stem=str(cast.stem),
    )
    parts = shlex.split(s, posix=False)

    def _strip_cmd_quotes(tok: str) -> str:
        t = tok.strip()
        while len(t) >= 2 and t[0] == '"' and t[-1] == '"':
            t = t[1:-1]
        return t.strip('"')

    return [_strip_cmd_quotes(p) for p in parts]


@dataclass(frozen=True, slots=True)
class ConvertResult:
    cast: SeabirdCastSet
    out_cnv: Path
    ok: bool
    message: str
    command: list[str] | None = None


def convert_cast_sets(
    casts: Iterable[SeabirdCastSet],
    *,
    out_dir: Path,
    command_template: str,
    overwrite: bool = False,
    timeout_s: int = 600,
    dry_run: bool = False,
) -> list[ConvertResult]:
    """
    Convierte cada cast a .cnv ejecutando un comando externo.

    Nota: la conversión real (hex->cnv) depende del software Sea-Bird.
    Este agente solo orquesta, valida y deja trazabilidad.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ConvertResult] = []

    for cast in casts:
        out_cnv = out_dir / f"{cast.stem}.cnv"
        if out_cnv.exists() and not overwrite:
            results.append(
                ConvertResult(cast=cast, out_cnv=out_cnv, ok=True, message="Ya existía (skip).", command=None)
            )
            continue

        # DatCnvW suele fallar si `/o` apunta fuera de la carpeta del cast; generamos en
        # `data/raw` (misma carpeta que .hex) y movemos el resultado a `data/cnv/`.
        staging_cnv = cast.hex_path.parent / f"{cast.stem}_ieo_tmp.cnv"
        if staging_cnv.exists() and overwrite:
            try:
                staging_cnv.unlink()
            except OSError:
                pass

        cmd = build_command_from_template(template=command_template, cast=cast, out_cnv=staging_cnv)
        if dry_run:
            results.append(ConvertResult(cast=cast, out_cnv=out_cnv, ok=True, message="Dry-run (no ejecutado).", command=cmd))
            continue

        work_dir = str(cast.hex_path.parent.resolve())
        _sub_kw: dict = {}
        if sys.platform == "win32":
            # Evita que DatCnvW abra ventana de consola/GUI y se quede esperando interacción.
            _sub_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            try:
                proc = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=max(5, int(timeout_s)),
                    cwd=work_dir,
                    shell=False,
                    **_sub_kw,
                )
            except PermissionError:
                if sys.platform != "win32":
                    raise
                proc = subprocess.run(
                    subprocess.list2cmdline(cmd),
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=max(5, int(timeout_s)),
                    cwd=work_dir,
                    **_sub_kw,
                )
        except FileNotFoundError:
            exe_guess = cmd[0] if cmd else "(vacío)"
            results.append(
                ConvertResult(
                    cast=cast,
                    out_cnv=out_cnv,
                    ok=False,
                    message=(
                        f"No se encontró el ejecutable de conversión ({exe_guess}). "
                        "Instala Sea-Bird SBE Data Processing o pasa --command-template con la ruta a DatCnvW.exe."
                    ),
                    command=cmd,
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            results.append(
                ConvertResult(
                    cast=cast,
                    out_cnv=out_cnv,
                    ok=False,
                    message=f"Excepción: {type(exc).__name__}: {exc}",
                    command=cmd,
                )
            )
            continue

        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()[:2000]
            results.append(ConvertResult(cast=cast, out_cnv=out_cnv, ok=False, message=f"Falló (code={proc.returncode}): {msg}", command=cmd))
            continue
        produced = staging_cnv
        if not produced.exists():
            alt = cast.hex_path.parent / f"{cast.stem}.cnv"
            if alt.exists():
                produced = alt
        if not produced.exists():
            msg = (proc.stderr or proc.stdout or "").strip()[:2000]
            results.append(
                ConvertResult(
                    cast=cast,
                    out_cnv=out_cnv,
                    ok=False,
                    message=(
                        f"No se encontró salida CNV (esperado {staging_cnv.name} o {cast.stem}.cnv) en "
                        f"{cast.hex_path.parent}. Salida DatCnv: {msg}"
                    ),
                    command=cmd,
                )
            )
            continue
        try:
            out_cnv.parent.mkdir(parents=True, exist_ok=True)
            if out_cnv.exists() and overwrite:
                out_cnv.unlink()
            shutil.move(str(produced), str(out_cnv))
        except OSError as exc:
            results.append(
                ConvertResult(
                    cast=cast,
                    out_cnv=out_cnv,
                    ok=False,
                    message=f"No se pudo mover el CNV a {out_cnv}: {exc}",
                    command=cmd,
                )
            )
            continue
        results.append(ConvertResult(cast=cast, out_cnv=out_cnv, ok=True, message="OK.", command=cmd))

    return results

