# Contrato de datos · Radiales Cudillero (visor + pipeline)

Este documento describe las reglas que implementa `src/ieo/validation/radial_contract.py`.

## Dónde se aplica

| Entrada | Momento |
|---------|---------|
| CSV en **Streamlit** (`run/app.py`) | Tras construir la serie mensual interpolada a la profundidad objetivo y **antes** de Marcos+ATAC. Si hay violaciones **ERROR**, el gráfico **no se muestra** salvo checkbox explícito de diagnóstico. |
| Parquet **canónico** (`*.ctd_canonical.parquet`) | Paso **01b** del pipeline (`run/main.py`): informe en `outputs/runs/<run>/checkpoints/` y mensaje en stderr si hay errores. |

## Umbrales por defecto (`RadialContractThresholds`)

Ajustables en código según región / producto.

### Perfil (filas)

- **Temperatura**: valores en \([-2, 32]\) °C (Cantábrico superficial / orden de magnitud).
- **Salinidad**: \([0, 42]\) PSU.
- **Saltos verticales** entre niveles adyacentes ordenados por profundidad (Δz ≤ 15 m): aviso/error si |ΔT| supera 4 / 8 °C.

### Serie mensual agregada

- **Saltos** entre dos puntos **consecutivos en el tiempo** (tras ordenar por fecha): aviso 6 °C, **error 12 °C** (temperatura).
- **Deriva fina** (solo temperatura): pendiente de la **mediana anual** vs año; aviso si |pendiente| ≥ 0,25 °C/año; error si ≥ 0,6 °C/año.
- **Ventanas**: diferencia de medianas anuales entre los últimos 3 años y los 3 anteriores; aviso si |Δ| ≥ 0,8 °C.

## Limitaciones declaradas

- Los saltos “mes a mes” usan **puntos consecutivos en la serie observada**; si faltan meses, el salto puede abarcar más de un mes calendario.
- Variables no térmicas / no salinas no disparan estas reglas (se puede ampliar `infer_variable_kind`).
