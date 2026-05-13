"""Validación / contrato de datos para radiales (perfil + serie mensual)."""

from .radial_contract import (
    Violation,
    ViolationSeverity,
    format_violations_markdown,
    infer_variable_kind,
    validate_canonical_ctd_polars,
    validate_monthly_radial_series,
    validate_profile_dataframe,
)
__all__ = [
    "Violation",
    "ViolationSeverity",
    "format_violations_markdown",
    "infer_variable_kind",
    "validate_canonical_ctd_polars",
    "validate_monthly_radial_series",
    "validate_profile_dataframe",
]
