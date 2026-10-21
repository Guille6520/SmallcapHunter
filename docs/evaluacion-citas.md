# Evaluación del verificador de citas

Cómo se midió si los modelos se inventan las citas, qué cambió entre el
protocolo 1 y el 2, y qué se puede (y qué no) concluir. Los datos son los de
la tabla `auditorias`; el desglose por tipo está guardado en el campo
`verificacion_citas.detalle` de cada análisis.

## La pregunta

El Detective y el Auditor justifican su tesis con citas del texto (MD&A del
10-Q, eventos 8-K, 13D/G). La pregunta es sencilla: **¿esa cita existe en el
documento que el modelo leyó?** Se comprueba sin otro LLM, comparando contra el
texto original. No dice si la interpretación es correcta, solo si la
evidencia está ahí.

## Qué había (protocolo 1)

- El prompt pedía citar "literalmente" pero también "responde en español",
  así que muchos modelos traducían la cita.
- El verificador exigía que la cita fuera una subcadena exacta del texto.
  Con menos de la mitad exactas, el análisis se marcaba `ALUCINACION` y esa
  etiqueta **sustituía** el veredicto real del modelo.

Resultado: 10 de 21 análisis de Claude y 20 de 51 de Gemini quedaban
marcados. Al leer a mano las citas "fallidas" de un lote de análisis reales,
la mayoría no eran inventadas.

## Qué se cambió (protocolo 2)

**Prompt.** La cita (`cita_literal`) va en el idioma del documento, sin
traducir, sin `...` y sin citar los scores del propio sistema. La traducción
va aparte (`cita_es`) y es lo que se enseña en el dashboard, marcada como
"sin verificar".

**Verificador** ([verificador_citas.py](../verificador_citas.py)). Cada cita cae
en una sola categoría:

| Categoría | Qué significa | ¿Con soporte? |
|---|---|---|
| `exacta` | Subcadena literal (la regla de la v1) | Sí |
| `exacta_sin_puntuacion` | Igual, ignorando puntuación y guiones tipográficos | Sí |
| `fragmentos` | Trozos unidos con `...`, cada uno exacto | Sí |
| `insiders` | Línea de compras que coincide con el Form 4 (importe y fecha) | Sí |
| `editada` | Casi literal: al menos el 85 % de sus trigramas juntos en el texto | Sí |
| `tabla` | Fila de tabla recompuesta: sus cifras están a menos de 40 tokens | Sí |
| `parafrasis` | Solo se parece en parte | No |
| `traducida` | Texto en español: no se puede comprobar | No |
| `score_del_sistema` | Cita un score nuestro, no el documento | No |
| `sin_soporte` | No aparece nada parecido | No |

Se guardan dos porcentajes por análisis: el **literal** (solo `exacta`, el
mismo número que medía la v1) y el **de soporte**. La etiqueta `ALUCINACION`
sale por debajo del 50 % con soporte; entre el 50 y el 80 % solo hay un aviso
de "citas débiles". El modelo conserva su veredicto en el JSON.

## Resultados

Sobre las 25 candidatas actuales, último análisis de cada modelo con el
protocolo 2.

| | Claude | Gemini |
|---|---|---|
| Análisis | 25 | 25 |
| Citas | 168 | 288 |
| Con soporte (media) | 96 % | 95 % |
| Literales (media) | 55 % | 63 % |
| Marcados `ALUCINACION` | 0 | 0 |
| "Citas débiles" | 0 | 1 |

Distribución por tipo de cita:

| Tipo | Claude | Gemini |
|---|---|---|
| `exacta` | 93 (55 %) | 186 (65 %) |
| `insiders` | 27 (16 %) | 32 (11 %) |
| `editada` | 20 (12 %) | 11 (4 %) |
| `exacta_sin_puntuacion` | 9 (5 %) | 11 (4 %) |
| `tabla` | 9 (5 %) | 8 (3 %) |
| `fragmentos` | 3 (2 %) | 26 (9 %) |
| `parafrasis` | 3 (2 %) | 11 (4 %) |
| `sin_soporte` | 4 (2 %) | 3 (1 %) |

Dos diferencias de estilo: Gemini sigue uniendo fragmentos con `...` (26
citas) aunque el prompt lo prohíbe, y Claude reescribe más (20 citas
`editada`). Al verificador le da igual, pero explica por qué las literales
son más bajas en Claude.

### Cuánto es del verificador y cuánto del prompt

Para separarlo, el lote del 14-oct (10 análisis de Claude y 7 de Gemini, con
el prompt antiguo) se volvió a puntuar con el verificador nuevo, sin llamar a
ningún modelo:

| Análisis marcados | Verificador v1 | Verificador v2 (mismas respuestas) |
|---|---|---|
| Claude | 6 de 10 | 1 de 10 |
| Gemini | 2 de 7 | 2 de 7 |

En Claude, casi toda la caída viene de medir mejor. En Gemini, el
verificador no cambia nada y la mejora (2 de 7 a 0 de 25) es del prompt. La
única métrica idéntica en los dos protocolos es el porcentaje de citas
literales: 47 % a 55 % en Claude, 50 % a 63 % en Gemini.

## El Auditor

Con el protocolo 2, el Auditor de Claude (que revisa a Gemini) empeoró el
veredicto en las 25 auditorías. El de Gemini (que revisa a Claude) confirmó
9, empeoró 12 y mejoró 4. Las citas nuevas de los Auditores tienen un
soporte parecido al de los Detectives (95 % y 94 %).

Los Detectives, por su parte, reparten igual sus veredictos (9
MUY_INTERESANTE y 16 INTERESANTE cada uno) y coinciden en 13 de 25
empresas. Eso deja dos explicaciones para que Claude empeore siempre: que
los análisis de Gemini sean peores, o que Claude, como auditor, empeore casi
todo lo que le llega.

### El experimento que las separa

Para decidirlo, el mismo Auditor de Claude, con el mismo prompt, revisó
también los análisis de **Claude** (`experimento_auditor.py`, 24 empresas; la
25.ª falló y no se contó). El resultado, guardado en
`resultados_experimento_auditor.csv`:

| Claude audita a... | Empeora el veredicto | Dice que la tesis no se sostiene |
|---|---|---|
| Gemini (producción) | 25 de 25 | — |
| Claude (experimento) | 24 de 24 | 24 de 24 |

Si fueran los análisis de Gemini los peores, Claude no habría empeorado
también los suyos. Es el auditor el que empeora todo, y de forma tan
uniforme que su veredicto casi no distingue unos análisis de otros.

Un detalle más: de esos 24, en 8 el veredicto del Auditor era el mismo que el
del Detective (INTERESANTE a INTERESANTE) y aun así el campo
`relacion_con_veredicto_detective` decía EMPEORA. La etiqueta que da el modelo
no es fiable; la relación se puede calcular en código comparando los dos
veredictos.

Qué NO prueba: que el Auditor de Gemini sea el bueno (confirma, empeora y
mejora, pero no sabemos si acierta), ni que los Detectives acierten. Solo que
el Auditor de Claude, tal como está, no aporta información.

## Límites

- **Muestras pequeñas** (25 empresas por modelo) y **no emparejadas**: el
  protocolo 1 son análisis históricos de otras empresas y otras fechas.
- **El verificador es indulgente a propósito** con la forma (guiones, `...`,
  tablas). Con las categorías `editada` y `tabla` podría dar por buena una
  cita que no lo es; la regla de tablas, por ejemplo, acepta cifras que
  coinciden en una zona cercana sin comprobar que sean la misma fila.
- **Un falso negativo conocido:** una cita real que resume una tabla del
  13D (tres porcentajes) quedó `sin_soporte` porque sus cifras están lejos
  entre sí en el texto.
- **No mide si la tesis es cierta.** Una cita real con una explicación
  inventada pasa.
- **No se ha contrastado con el precio** de las acciones, que es lo que
  decidiría si los veredictos sirven.

## Cómo reproducirlo

Todo sale de `auditorias`. Por protocolo (los análisis sin marca son del
protocolo 1):

```sql
select modelo_llm,
       coalesce((respuesta_llm->>'version_protocolo')::int, 1) as protocolo,
       count(*) as analisis,
       round(avg((verificacion_citas->>'porcentaje_verificado')::numeric)) as pct_literal,
       round(avg((verificacion_citas->>'porcentaje_soportado')::numeric)) as pct_soporte,
       count(*) filter (where veredicto = 'ALUCINACION') as marcados
from auditorias
where respuesta_llm ? 'veredicto_preliminar' and verificacion_citas is not null
group by 1, 2 order by 1, 2;
```

La misma tabla, con los gráficos, está en la pestaña Observabilidad del
dashboard. Para volver a puntuar análisis ya guardados con el verificador
actual, basta llamar a `verificar_citas(citas, texto_fuente, transacciones)`
sobre el JSON guardado: no necesita red ni modelos.
