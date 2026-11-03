# Tesis V2: posición en el rango de 52 semanas (pre-registro)

Este documento se escribe **antes** de mirar ningún resultado de V2, y queda fijado
en el historial de git (el hash del commit es su sello de fecha). Si algo de lo que
pone aquí cambia durante el seguimiento, no se edita: se abre una versión nueva (V3).

Fecha de inicio: 2-nov-2026. Horizonte de evaluación: 12 y 24 meses.

## La pregunta

El score de la Capa 2 premia hoy estar **cerca de mínimos** de 52 semanas
(`score_precio` de `scorer_capa2.py`: 10 puntos en mínimos, 0 en máximos). La razón
original era que un insider que compra cuando la acción está hundida tiene más
convicción. Dos cosas ponen esa decisión en duda:

- La literatura sobre el máximo de 52 semanas (George y Hwang, 2004) encuentra que
  la cercanía a **máximos** predice mejor retorno, justo al revés de nuestro score.
  Lo hace para el mercado en general, no para insiders comprando en small caps.
- Un análisis exploratorio sobre 274 eventos históricos del propio proyecto
  encontró una relación positiva débil entre la posición en el rango y el retorno
  posterior (a 12 meses no resistía la corrección por pruebas múltiples; a 24 sí),
  y que lo peor era el tercio pegado a mínimos. Es una **hipótesis**, no un
  resultado: muestra pequeña, con sesgo de supervivencia, mirada después de ver
  los datos.

**Pregunta de V2:** ¿rinden más, a 12 y 24 meses, las candidatas que el score
selecciona cuando se invierte la lectura de la posición en el rango (premiar la
cercanía a máximos) que cuando se premia la cercanía a mínimos?

## Los tres brazos

Todo es idéntico entre brazos salvo `score_precio` (0-10). Capa 1, los otros tres
sub-scores (convicción, temporal, catalizador), el corte de 25 sobre 40 y el
análisis con modelos son los mismos.

| Brazo | `score_precio` | Qué prueba |
|---|---|---|
| **V1** (actual) | 10 si posición ≤ 0,15; 8 si ≤ 0,30; 6 si ≤ 0,45; 4 si ≤ 0,60; 2 si ≤ 0,80; 0 por encima | La tesis original: cerca de mínimos |
| **V2** | 10 si posición ≥ 0,85; 8 si ≥ 0,70; 6 si ≥ 0,55; 4 si ≥ 0,40; 2 si ≥ 0,20; 0 por debajo | La inversa: cerca de máximos y no en caída libre |
| **V0** (control) | 5 siempre | Sin posición: ¿aporta algo cualquiera de las dos lecturas? |

La posición es `(precio − mínimo52) / (máximo52 − mínimo52)`, de 0 (en mínimos) a 1
(en máximos), tal como la calcula `mercado_yfinance.py`. Si no hay dato, V1 y V2
dan 0 y V0 da 5.

V2 es el espejo exacto de V1 a propósito (`score_precio_v2(p) = score_precio_v1(1 − p)`): es la forma más simple de la hipótesis
contraria y la más fácil de explicar. No se ajustan los tramos a los datos.

## Protocolo

1. **Instantánea.** Cada vez que se ejecuta `scorer_v2.py` se guarda, para las
   empresas puntuadas, su posición, los sub-scores, los tres totales y qué brazos la
   marcan como candidata (total ≥ 25). Sin cambiar nada del pipeline de producción
   ni de V1.
2. **Congelado.** Las reglas de arriba no se tocan durante el seguimiento. Cualquier
   cambio es otra versión.
3. **Calendario.** Una instantánea por pasada del orquestador (cada 48 h como
   mucho). Para evaluar, cada empresa que entra por primera vez en un brazo cuenta
   desde esa fecha.
4. **Qué se evalúa (uso personal).** Con los precios que ya baja el seguimiento
   privado (`MOSTRAR_SEGUIMIENTO=1`): exceso sobre el IWM a 12 y 24 meses desde la
   primera instantánea en que cada brazo la marcó, mediana y % que dobla. Esos
   resultados **no se publican**: el proyecto es informativo, no un registro de
   rentabilidad.

## Qué contaría como evidencia a favor o en contra

- **A favor de V2:** la diferencia de medianas de exceso a 12 meses entre V2 y V1
  es positiva con un intervalo al 95 % (bootstrap por empresa) que excluye el 0, y
  se mantiene a 24 meses.
- **En contra de V2 (o de cualquier lectura de la posición):** el intervalo incluye
  el 0 a 12 y 24 meses, o V0 (sin posición) rinde igual que V1 y V2.
- **No concluyente:** cualquier otro caso. Es el desenlace más probable.

## Límites que se aceptan de antemano

- **Poca potencia.** Cada brazo tendrá del orden de 25 candidatas, muchas
  compartidas entre brazos, y las instantáneas sucesivas repiten empresas. Con esa
  muestra solo se verían diferencias enormes (decenas de puntos). Por eso **no se
  espera concluir en 12 meses**: el valor está en el registro honesto y en acumular
  años.
- **Dependencia temporal.** Todas las candidatas comparten el mismo mercado y el
  mismo régimen. Los intervalos son optimistas.
- **Sin deslistadas resueltas.** Una empresa que deje de cotizar durante el
  seguimiento no tiene precio en Yahoo; se contará y se avisará, no se ignorará en
  silencio.
- **Una sola hipótesis.** Se prueba esta y solo esta. Si miramos otras (más
  variantes del score), cuentan como pruebas múltiples y se corrige.

## Qué hay en la rama `tesis-v2`

- `scorer_v2.py`: calcula los tres totales y guarda la instantánea (tabla
  `tesis_instantaneas`, creada por el propio script).
- `tests/test_tesis_v2.py`: los tramos de V2, el control V0 y la comparación de
  conjuntos.
- El resto del repositorio es el de `main`. V2 no modifica los análisis ni las
  puntuaciones de V1: las lee y las compara.
