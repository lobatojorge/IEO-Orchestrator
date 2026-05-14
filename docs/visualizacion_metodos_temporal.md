# Métodos de visualización para series temporales (documento temporal)

Este documento recopila varias opciones para **visualizar una serie temporal con estacionalidad** (ciclo anual), **tendencia** (cambio a largo plazo) y **residuos** (variación no explicada por el modelo).  
Cada opción se presenta con:

- **De qué datos parte**
- **Qué cálculos hace**
- **Qué estamos viendo en la gráfica**
- **Ventajas**
- **Inconvenientes**

La idea es elegir un punto medio: suficientemente simple para ser entendible y mantenible, pero suficientemente sólido para interpretar cambios reales en el sistema.

---

## Datos de partida (comunes a todas las opciones)

En nuestro caso, el punto de partida típico es un Excel crudo (copia de seguridad en `data/raw/`) que, tras normalizarlo a un formato procesable, acaba entrando al pipeline como `.cnv` (en `data/cnv/`) o como una tabla ya tabular.

- **3 estaciones** (a distinta distancia de costa).
- **Perfiles verticales**: temperatura medida a múltiples profundidades en una misma fecha de muestreo.
- **Frecuencia mensual** (en la práctica puede haber meses sin datos y, ocasionalmente, más de un muestreo dentro del mismo mes).

Para poder aplicar las visualizaciones de este documento, convertimos ese formato “perfil por muestreo” a una **serie temporal mensual por estación** y (normalmente) para una **profundidad objetivo** (por ejemplo, 5 m).

En todos los métodos asumimos que, tras esa preparación, disponemos como mínimo de una tabla con:

- **fecha**: fecha mensual (por ejemplo, primer día de cada mes).
- **valor observado**: temperatura mensual (por ejemplo, temperatura estimada a 5 m) para una estación concreta.

Opcionalmente (si lo calculamos):

- **IC95 del observado**: intervalo de confianza al 95% del valor observado mensual (una medida de incertidumbre asociada a la media mensual).

### ¿Cómo se obtiene una serie mensual si los muestreos son irregulares?

La preparación suele seguir estos pasos:

1. **Elegir estación**: filtrar una estación (radial cercana, media, lejana, según el diseño).
2. **Elegir profundidad objetivo**: por ejemplo, 5 m.
   - Si el CTD no tiene exactamente 5 m, se interpola dentro del perfil para estimar la temperatura a esa profundidad.
3. **Agrupar por mes**: si dentro de un mismo mes hay varios muestreos, se calcula la **media mensual**.
4. **Malla mensual completa**: si hay meses sin muestreo, se dejan como ausentes (sin inventar observaciones).

Después, cada método decide cómo tratar los meses ausentes durante el ajuste:

- **Ajustar solo con meses observados** y usar el modelo para predecir sobre una malla mensual completa (útil para bandas y pronóstico).
- O bien **restringir la visualización** a los meses observados (más simple, pero menos útil para “continuidad”).

---

## Opción 1: Tendencia lineal + estacionalidad mensual fija (estilo `marcos.R` con regresión lineal)

### Mini-esquema (orientativo)

**Panel A (serie y ajuste)**  

    valor
     ▲          /\      /\      /\      /\
     │         /  \    /  \    /  \    /  \
     │  • • • /    \• /    \• /    \• /    \• •
     │       /      \/      \/      \/      \
     │      ───────────────────────────────────  tendencia (recta)
     └────────────────────────────────────────► tiempo (meses)

**Panel B (residuos)**  

    residuo
      ▲   •   •    •
      │ •   •   •
      │    •     •
      └───────────────────────────────► tiempo

### De qué datos parte

- Serie mensual: **fecha** y **temperatura mensual** (por ejemplo, a 5 m).

### Qué cálculos hace

1. **Tendencia lineal**: ajusta una recta en el tiempo (una tasa constante de cambio).
2. **Estacionalidad mensual**: asigna un “efecto” distinto a cada mes (enero, febrero, …, diciembre).
3. **Valor esperado**: suma “tendencia + efecto del mes”.
4. **Residuo**: observado − esperado.

En términos sencillos: “cuánto sube o baja a largo plazo” + “cómo suele variar dentro del año” + “lo que no encaja”.

### Qué estamos viendo en la gráfica

- **Serie observada**: puntos o línea con los valores mensuales.
- **Tendencia**: una recta.
- **Ciclo estacional**: se refleja en la onda anual (porque cada mes tiene un ajuste propio).
- **Residuos**: variaciones mes a mes que el modelo no explica.

### Ventajas

- **Muy explicable**: se puede describir claramente qué hace y qué significa cada parte.
- **Interpretación directa**: la pendiente de la recta se interpreta como “cambio por año”.
- **Robusto y barato**: rápido de ejecutar, pocas dependencias.

### Inconvenientes

- **La tendencia es siempre una recta**: si el cambio se acelera o se frena con los años, el modelo lo simplifica demasiado.
- **El ciclo mensual es fijo**: asume que “el patrón de meses” es el mismo todos los años.

---

## Opción 2: Tendencia suave + estacionalidad mensual fija (estilo `marcos.R` con tendencia no lineal)

### Mini-esquema (orientativo)

**Panel A (serie y ajuste)**  

    valor
     ▲          /\      /\      /\      /\
     │         /  \    /  \    /  \    /  \
     │  • • • /    \• /    \• /    \• /    \• •
     │       /      \/      \/      \/      \
     │     _/────────────────────────────────\_  tendencia (curva suave)
     └────────────────────────────────────────► tiempo (meses)

**Panel B (residuos)**  

    residuo
      ▲  • •   •
      │ •   •    •
      │    •
      └───────────────────────────────► tiempo

### De qué datos parte

- Serie mensual: **fecha** y **temperatura mensual** (por ejemplo, a 5 m).

### Qué cálculos hace

Igual que la opción 1, salvo que:

- La **tendencia** no se fuerza a ser una recta. Se ajusta como una **curva suave** (una línea que puede doblarse de forma controlada).
- La **estacionalidad** sigue siendo mensual (enero–diciembre).

### Qué estamos viendo en la gráfica

- Serie observada.
- Tendencia como curva suave.
- Ciclo estacional anual.
- Residuos.

### Ventajas

- **Más realista** cuando la serie muestra periodos con ritmos de cambio diferentes.
- Sigue siendo **interpretables**: “tendencia a largo plazo” y “ciclo anual”.

### Inconvenientes

- Requiere elegir **cuánta flexibilidad** darle a la curva (si es demasiado flexible puede seguir el ruido; si es demasiado rígida se parece a una recta).
- La tasa de cambio deja de ser un único número simple (depende del periodo).

---

## Opción 3: Estacionalidad continua con armónicos + componente autoregresiva (lo que ya discutimos: Fourier + AR(1))

> **Armónicos**: combinación de senos y cosenos para representar un ciclo anual suave.  
> **AR(1)**: modelo autoregresivo de orden 1; significa que el valor de un mes tiende a parecerse al del mes anterior (persistencia).

### Mini-esquema (orientativo)

**Panel A (ciclo suave + anomalía)**  

    valor
     ▲        ~~~~~~~~  ciclo anual suave  ~~~~~~~~
     │  • •  ~      ~•~      ~•~      ~•~      ~ •
     │     ~~        ~~        ~~        ~~        ~~
     └────────────────────────────────────────► tiempo

**Panel B (anomalía con persistencia)**  

    anomalía
      ▲   ••••  (meses seguidos con signo parecido)
      │ ••   ••
      └───────────────────────────────► tiempo

### De qué datos parte

- Serie mensual: **fecha** y **temperatura mensual**.

### Qué cálculos hace

1. Ajusta un **ciclo anual suave** (senos/cosenos).
2. Resta ese ciclo a la serie para obtener una **serie sin estacionalidad**.
3. Ajusta un **modelo autoregresivo de primer orden** sobre esa serie (o sobre una anomalía mensual).
4. Genera un **pronóstico corto** con intervalos.

### Qué estamos viendo en la gráfica

Depende de cómo se diseñe, pero normalmente:

- El ciclo anual suave ajustado.
- La serie “sin estacionalidad”.
- La parte autoregresiva (persistencia) y su pronóstico.

### Ventajas

- El ciclo anual queda **suave y continuo** (no “a saltos” por meses).
- El componente autoregresivo es útil para pronóstico a corto plazo.

### Inconvenientes

- Más difícil de explicar que “un efecto por mes” (hay que explicar senos y cosenos).
- Si el objetivo principal es **descomposición visual clara** en tendencia/estacionalidad/residuo, no es la opción más directa.

---

## Opción 4: Inspirada en ATAC (WGINOR/ICES) + estacionalidad tipo Marcos (recomendación para “punto medio”)

> **ATAC**: “Analysis of Trends And recent Changes”, una metodología para estimar tendencia y detectar si los años/meses recientes se apartan de lo esperable según el pasado, usando bandas de incertidumbre y pronósticos.  
> **Bandas**: intervalos (por ejemplo, 95%) que muestran el rango “esperable” según el modelo.

### Mini-esquema (orientativo)

**Panel 1 (observación + tendencia + bandas + tramo reciente destacado)**  

    valor
     ▲  •  • •   •  •
     │   •  •  •
     │  ███████████████████████  banda histórica (rango esperable)
     │      ────────────────     tendencia estimada
     │                      ┆▌▌▌ tramo reciente (comparación con forecast)
     └──────────────────────┼──────────────────► tiempo
                            └─ “ventana reciente”

**Panel 2 (residuos)**  

    residuo
      ▲  • •  •   •
      │ •  •   •
      └────────────────────────────► tiempo

**Panel 3 (serie completa panorámica)**  

    valor
     ▲  • • • • • • • • • • • • • • • • • • • •
     └────────────────────────────────────────► tiempo

### De qué datos parte

- Serie mensual: **fecha** y **temperatura mensual** (por ejemplo, a 5 m).
- Idealmente, con un IC95 del observado mensual (si lo calculamos).

### Qué cálculos hace

1. Ajusta **tendencia** (preferiblemente suave) y **estacionalidad mensual** (como en `marcos.R`).
2. Calcula **bandas históricas** de lo “esperable” según el modelo.
3. Ajusta el modelo **sin los últimos N meses** (por ejemplo, N=12).
4. Con ese modelo truncado, hace un **pronóstico** de esos N meses y dibuja sus **bandas** (por ejemplo, 50%, 75%, 95%).
5. Compara las observaciones reales con las bandas: si caen fuera, indica **cambio reciente** respecto al comportamiento pasado.

### Qué estamos viendo en la gráfica

En un único “panel 1” se puede ver:

- Dónde están las observaciones.
- Cuál es la tendencia a largo plazo (línea).
- Qué rango de valores sería razonable según el modelo (bandas).
- Si lo reciente está o no dentro de lo esperable.

Y abajo:

- Residuos (panel 2): para ver meses atípicos sin mezclarlo con la estacionalidad.
- Serie completa (panel 3): como referencia panorámica.

### Ventajas

- **Muy explicable**: “modelamos tendencia y ciclo anual; luego comprobamos si lo reciente se aparta del patrón histórico”.
- **Alineado con WGINOR/ICES** en forma de presentar incertidumbre y cambio reciente.
- Mantiene la estacionalidad (clave para series mensuales costeras).

### Inconvenientes

- Hay que ser cuidadoso con el lenguaje: las bandas muestran “rango esperable según el modelo”, no una certeza.
- Si hay muchos meses ausentes, el ajuste y la comparación de la ventana reciente pueden volverse menos estables.

---

## Recomendación de selección (para no “matar moscas a cañonazos”)

Si el objetivo es **máxima claridad y mínimo coste**, empezar por:

- **Opción 2 (tendencia suave + estacionalidad mensual fija)** para descomponer de forma limpia.

Si además queremos una lectura “al estilo WGINOR” sobre **cambios recientes**, añadir:

- **Opción 4 (ATAC inspirada)** como capa visual y de comparación de la ventana reciente.

---

## Nota de trazabilidad (qué ideas vienen de dónde)

- La descomposición “tendencia + ciclo mensual + residuos” viene de `references/marcos.R`.
- La presentación con bandas, y el énfasis en “cambios recientes” viene de `references/WGINOR/TAF_ATAC` (metodología ATAC).

