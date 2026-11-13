# Las vueltas que ha dado este proyecto

Esto es lo que probé, lo que esperaba, lo que pasó de verdad y lo que decidí, con los números que tengo. Incluye lo que salió mal. Lo escribo para mí tanto como para quien lo lea: un proyecto que solo cuenta sus aciertos no enseña nada, y más de una vez el diseño actual es la consecuencia directa de haberme equivocado.

Cada vuelta termina con una línea de **virtud** (lo que me dejó) y otra de **coste** (lo que sigue mal o lo que me costó). Los números salen de mi base de datos, de los CSV del repo y de los scripts; cuando un número no se puede reproducir desde el repo, lo digo. Las fechas son de 2026.

Índice rápido: [1](#1-la-hipótesis-frente-a-los-datos-históricos) hipótesis frente a datos · [2](#2-el-hueco-del-segundo-modelo-sep) el segundo modelo · [3](#3-las-citas-protocolo-1-frente-a-protocolo-2-oct) citas · [4](#4-el-auditor-v1-daba-un-veredicto-y-era-ruido-oct) Auditor v1 · [5](#5-el-dashboard-enseñaba-el-veredicto-equivocado) bug de los 39 de 55 · [6](#6-dos-tesis-sobre-el-mismo-score-oct-nov) tesis V1 y V2 · [7](#7-un-auditor-de-dos-partes-prueba-sintética-oct) prototipo de dos partes · [8](#8-el-auditor-v2-en-producción-7-nov) Auditor v2 · [9](#9-el-veredicto-cambia-al-re-analizar) inestabilidad · [10](#10-robustez-copias-y-alertas-sep-nov) robustez · [11](#11-fallos-de-datos-y-de-concurrencia) fallos de datos · [12](#12-un-fallo-en-el-score-de-aceleración-encontrado-al-documentar-el-código-7-nov) score de aceleración · [13](#13-etiquetar-a-mano-lo-que-marca-el-auditor-13-nov) etiquetas a mano · [Lo que todavía no sé](#lo-que-todavía-no-sé)

---

## 1. La hipótesis frente a los datos históricos

**Qué probé.** La idea original: cuando varios directivos de una small cap compran a la vez, es una señal temprana. Antes de construir la parte de modelos de lenguaje, quise saber si la señal sola ya funcionaba. Busqué todos los clusters de compras (3 o más insiders en 60 días) en mi histórico (`descubrir_clusters_historicos.py`), reconstruí la capitalización de cada empresa *en ese momento* para quedarme solo con las que eran de verdad small caps (`validacion_historica.py`) y medí su retorno frente al Russell 2000 (`backtest_rendimiento.py`).

**Qué esperaba.** Que la señal batiera al índice, con alguna holgura.

**Qué pasó.**

- De 4.305 clusters, solo pude validar **274**: 218 ya eran de otro tamaño, 415 no tenían precio histórico en Yahoo y 3.398 no tenían el dato de acciones en circulación de esa fecha. Me quedé con el 6 % de lo que encontré, y sesgado hacia empresas que siguen cotizando.
- Con esos 274 hice un análisis exploratorio que apuntaba en la misma dirección (exceso de retorno mediano negativo a 12 y 24 meses frente al IWM, y la mayoría de eventos por debajo del índice). **No conservo el script ni los resultados, así que no publico cifras: no las puedo reproducir.** `backtest_rendimiento.py` está pensado para esa pregunta pero solo escribe su resumen en el log.
- Recall sobre ganadoras (`validacion_recall_ganadoras.py`, `resultados_test_ganadoras.csv`): de 141 empresas que multiplicaron por 3 o más desde su mínimo como small cap, 86 (61 %) habían mostrado el cluster antes. Suena bien y no demuestra nada: no hay grupo de control, las ganadoras se eligen mirando el resultado y solo cuentan las que siguen vivas.
- Después medí si el veredicto del agente discriminaba (`backtest_capa3.py`, con la empresa anonimizada y los años enmascarados, con una versión anterior del Detective). En 57 casos (`resultados_ciego_mixtos.csv`): 46 `NADA_INTERESANTE`, 10 `INTERESANTE`, 1 `MUY_INTERESANTE`. El exceso mediano a 12 meses fue de -18 % en los dos grupos grandes: el veredicto no distinguía. En otra prueba con 4 empresas que acabaron subiendo mucho (`resultados_ciego_ganadoras.csv`), el agente dio `NADA_INTERESANTE` a las cuatro.

**Qué decidí.** Quitar de mi vocabulario la palabra «validado». Una versión de la descripción del repositorio decía «Validado contra 274 casos históricos frente al Russell 2000», y era engañosa: la validación no mostró que bate al índice. Pasé el proyecto de «detecta pre-explosivas» a «herramienta de investigación» y dejé de prometer nada sobre rentabilidad.

**Virtud.** Tengo la pregunta de fondo medida con sus límites en vez de una frase de marketing, y el resto del trabajo (calidad del análisis, verificabilidad) se justifica por sí mismo sin necesitar que la señal gane.

**Coste.** No tengo una respuesta a si la señal sirve. Las muestras son pequeñas, con sesgo de supervivencia, concentradas en 2022–2025 y con un mercado común. Y el mejor número que tengo (el del análisis exploratorio) no es reproducible desde el repo.

---

## 2. El hueco del segundo modelo (sep)

**Qué probé.** Los dos Detectives ciegos necesitaban dos modelos de familias distintas. Claude era uno; para el otro quería uno gratuito. Pasé por Groq con Llama 3.3 70B, luego GPT-OSS 120B (Llama dejó de estar accesible), luego OpenRouter con Nemotron (commits del 21 al 25 de septiembre).

**Qué esperaba.** Un modelo gratuito que diera JSON válido y citas reales casi siempre.

**Qué pasó.** Nemotron fallaba con JSON roto y con 429/503 del proveedor (y en una pasada real un 503 suelto y un JSON mal formado sacaron a dos modelos y 6 de 10 empresas quedaron sin analizar). Hice una prueba con 7 empresas, las que peor le habían ido, comparando candidatos (el script de la prueba no está en el repo):

| Modelo | JSON válido | Citas verificadas | Análisis con alucinación |
|---|---|---|---|
| Llama 4 Scout | 7 de 7 | 39 % | 4 de 7 |
| Nemotron (de pago, como control) | 6 de 7 | 66 % | 1 de 7 |
| Kimi K2 | 7 de 7 | 74 % | 0 de 7 |

Entre los gratuitos de OpenRouter, Gemma estaba saturado (429 constante), Qwen formateaba bien pero no analizaba (0 % de citas) y otro ya no existía.

**Qué decidí.** Sustituir el hueco por **Claude Haiku 4.5** (commit del 28 de septiembre), de pago pero a céntimos por llamada y fiable. Sin proveedores nuevos que mantener. También cambié la política del orquestador: un modelo ya no se retira por un fallo suelto sino tras 3 seguidos.

**Virtud.** El segundo modelo dejó de ser el eslabón débil. Y la prueba me enseñó que «JSON válido» no es «buen análisis»: Llama formateaba perfecto y alucinaba más de la mitad.

**Coste.** Ya no es gratis (unos 2 céntimos por llamada de Claude), la prueba fue de 7 casos, y Claude no estaba en esa tabla: Kimi K2 fue el mejor de los candidatos probados, pero implicaba otro proveedor y dejaba de ser gratis al acabarse el crédito. Elegí Claude porque ya lo usaba, sin otra clave ni otro proveedor que mantener, no porque lo hubiera ganado en una comparación directa.

---

## 3. Las citas: protocolo 1 frente a protocolo 2 (oct)

**Qué probé.** El verificador exigía que cada cita fuera una subcadena exacta del texto y marcaba `ALUCINACION` por debajo del 50 %. El prompt pedía citar «literalmente» y a la vez «responde en español».

**Qué esperaba.** Que las marcas señalaran modelos que se inventan datos.

**Qué pasó.** Con el protocolo 1 quedaban marcados 10 de 21 análisis de Claude (48 %) y 20 de 51 de Gemini (39 %). Al leer a mano las citas «fallidas», la mayoría no eran inventadas: eran traducciones al español, fragmentos unidos con `...`, guiones distintos, líneas de la lista de insiders (que no están en el MD&A) o filas de tabla recompuestas. Cambié el prompt (cita en inglés, traducción aparte, sin `...`) y el verificador (categorías: exacta, sin puntuación, fragmentos, insiders, editada, tabla, y las que no cuentan: parafraseada, traducida, score del sistema, sin soporte). Hoy, con el protocolo 2: 0 marcados de 31 análisis por modelo, 96 % de citas con soporte en los dos.

Para separar qué parte era del verificador y qué parte del prompt, volví a puntuar con el verificador nuevo las respuestas del prompt viejo (10 de Claude y 7 de Gemini), sin llamar a ningún modelo: Claude pasó de 6 de 10 marcados a 1 de 10; Gemini se quedó en 2 de 7. Es decir, en Claude casi toda la mejora es medir mejor; en Gemini, el prompt.

**Qué decidí.** Enseñar los dos porcentajes (literal estricto y con soporte) para no hacerme trampas: la regla nueva es más generosa, y la única cifra comparable sin truco es la de citas literales (47 % a 56 % en Claude, 50 % a 65 % en Gemini).

**Virtud.** Un verificador que entiende la forma real en que se cita, y una medida honesta de cuánto fue mejora y cuánto fue aflojar la vara. Detalle y consultas en [evaluacion-citas.md](evaluacion-citas.md).

**Coste.** El verificador comprueba que la evidencia **existe**, no que la interpretación sea correcta. Una cita real con una explicación inventada pasa, y es la razón de que exista el Auditor. Además, las categorías `editada` y `tabla` son indulgentes a propósito y podrían dar por buena una cita que no lo es. Las muestras son pequeñas y los dos protocolos no están emparejados (otras empresas, otras fechas).

---

## 4. El Auditor v1 daba un veredicto y era ruido (oct)

**Qué probé.** La primera idea de «segunda mirada»: un modelo lee la tesis del Detective y da su propio veredicto, diciendo si mejora, confirma o empeora el del Detective.

**Qué esperaba.** Que a veces confirmara, a veces corrigiera, y que su desacuerdo fuera informativo.

**Qué pasó.** Declaraba que empeoraba el veredicto casi siempre, y la realidad era menos extrema, pero igualmente mala. De las **130 auditorías v1** que hay en la base (las otras filas de la tabla `auditorias` son 168 análisis de Detective y 106 filas de score), el Auditor **declaró** `EMPEORA` en 96 (74 %), `CONFIRMA` en 22 (17 %) y `MEJORA` en 12 (9 %); por modelo, `EMPEORA` en 37 de 37 de Claude, 41 de 65 de Gemini y 18 de 28 del modelo del hueco antiguo. Pero esa etiqueta exagera: al emparejar cada auditoría con el análisis que auditaba y comparar los veredictos reales, el del Auditor fue **más bajo solo en 59 (45 %)**, igual en 47 (36 %) y más alto en 2 (el resto implica una `ALUCINACION`). Lo declarado (74 %) y lo real (45 %) difieren, y el hecho de que difieran ya es un hallazgo: la etiqueta que da el modelo no refleja lo que hace.

Para separar si los análisis de Gemini eran peores o si Claude, como auditor, empeora todo, hice que Claude auditara también sus **propios** análisis (`experimento_auditor.py`, `resultados_experimento_auditor.csv`, 24 empresas): **declaró `EMPEORA` y «la tesis no se sostiene» en 24 de 24**, aunque su veredicto fue más bajo en 16 e igual en 8. Un auditor que dice siempre que la tesis falla no discrimina. Con el protocolo 2, el Auditor de Claude (que revisa a Gemini) declaró `EMPEORA` en 31 de 31.

Intenté arreglarlo con el prompt (una tarea 3, de «fortalezas infravaloradas») y **no funcionó**: el sesgo siguió. Más tarde, otra causa: el Auditor no recibía los datos estructurados (compras de insiders, capitalización, scores) que el Detective sí cita, así que cualquier cifra que viniera de ahí le parecía inventada. Eso explica parte de su pesimismo, no todo.

**Qué decidí.** No seguir afinando el prompt. El problema era la tarea, no la redacción: un modelo no da un juicio global estable sobre el análisis de otro. La respuesta fue cambiar la tarea (vuelta 7 y 8).

**Virtud.** Un fallo medido, con un experimento que separa dos explicaciones (la de que los análisis fueran malos y la de que el auditor sesga), en lugar de una intuición.

**Coste.** Mientras estuvo en producción, ese Auditor tapó el veredicto de los Detectives (vuelta 5) y gastó cuota de Gemini en un resultado inútil. Las 130 auditorías v1 siguen en la base como histórico. Y un recordatorio sobre mí mismo: durante un tiempo hablé de «4 de cada 5» sin haber comparado los veredictos reales; la etiqueta declarada (74 %) y la comparación real (45 %) cuentan historias distintas, y tendría que haber mirado las dos desde el principio.

---

## 5. El dashboard enseñaba el veredicto equivocado

**Qué probé.** Mirar qué veredicto veía yo en la lista de candidatas.

**Qué esperaba.** El de los Detectives.

**Qué pasó.** El dashboard enseñaba «el último veredicto guardado». Como el Auditor guarda su fila después de los Detectives, en **39 de las 55 empresas** lo que se veía era el del Auditor, que empeoraba el del Detective casi siempre. Con ese criterio salía 1 de 55 empresas `MUY_INTERESANTE`; con los votos de los Detectives son 18 de 55 (las dos cifras, medidas de nuevo el 7-nov sobre la base).

**Qué decidí.** El veredicto de una empresa son los votos de sus Detectives (`votos.py`, con sus tests). La lista se ordena por el mejor voto, y el desacuerdo entre los dos modelos se ve en dos columnas. El Auditor queda como nota aparte.

**Virtud.** Un bug de diseño, no de código, que solo se vio al comparar lo que el sistema mostraba con lo que decían sus partes. Ahora el criterio está escrito y probado.

**Coste.** Estuve usando el dashboard con ese sesgo sin darme cuenta, y distorsionó mi lectura real de las candidatas durante semanas.

---

## 6. Dos tesis sobre el mismo score (oct-nov)

**Qué probé.** La Capa 2 premia estar cerca de mínimos de 52 semanas. Un análisis exploratorio sobre los 274 eventos históricos mostró una relación positiva débil entre la posición en el rango y el retorno posterior (a 12 meses no resistía la corrección por pruebas múltiples, a 24 sí), y que lo peor era el tercio pegado a mínimos. La literatura (George y Hwang, 2004) apunta en la misma dirección.

**Qué esperaba.** Una pista para mejorar el score, o un resultado nulo.

**Qué pasó.** Una pista débil, mirada después de ver los datos, con muestra pequeña y sesgo de supervivencia: una **hipótesis**, no un resultado. Y una pega con casos sueltos: las dos grandes subidas de la prueba ciega (OUST y MARA) estaban cerca de mínimos, lo que cuadra con la V1 y no con la V2. Con 4 casos no prueba nada, y justamente por eso no quise elegir bando mirando ejemplos.

**Qué decidí.** No cambiar el score en producción. Abrir la rama [`tesis-v2`](https://github.com/Guille6520/SmallcapHunter/tree/tesis-v2) con la tesis contraria (premiar máximos, espejo exacto de la V1, sin ajustar tramos a los datos), **pre-registrada** antes de ver resultados (el hash del commit es el sello de fecha), con un control (V0, sin posición) y los criterios de qué contaría como evidencia a favor o en contra. Cada pasada guarda una instantánea de qué empresas marca cada tesis.

**Qué pasó después.** La primera instantánea (3-nov): 26 candidatas en V1, 11 en V2 y 4 en las dos. Son tesis distintas, no una mejora medida. Ojo: es anterior a la corrección del score de aceleración de la vuelta 12, así que hay que repetirla.

**Virtud.** Una forma de no engañarme: la hipótesis y los criterios están fijados antes de mirar nada, y el desenlace más probable (no concluyente) está escrito de antemano.

**Coste.** Poca potencia: con unas decenas de candidatas por brazo solo se verían diferencias enormes, y no se espera concluir en 12 meses. Es una inversión de tiempo sin garantía de respuesta. Los resultados por empresa no se publican.

---

## 7. Un auditor de dos partes: prueba sintética (oct)

**Qué probé.** Cambiar la tarea. En vez de pedir un juicio global, dividir el Auditor en dos partes (prototipo en `pruebas_auditor/`, carpeta fuera de git por llevar texto de la SEC): (A) un programa saca cada cifra de la tesis y comprueba que aparezca en el texto original; (B) un modelo revisa afirmación por afirmación con una cita literal, que luego verifica el verificador de citas.

Para medirlo preparé 20 análisis reales y a 10 les cambié a mano **una cifra** por otra falsa. Un buen auditor protesta mucho más en los 10 alterados que en los 10 limpios.

**Qué esperaba.** Que el modelo hiciera la mayor parte del trabajo.

**Qué pasó.**

| | Detecta la cifra falsa | Falsas alarmas |
|---|---|---|
| Auditor v1 (opinión global) | no distingue: «empeora» siempre | |
| Solo código, primera versión | 10 de 10 | 3 de 10 |
| Solo código, con los datos estructurados del Detective | 10 de 10 | 2 de 10 |
| Código y modelo (Claude Haiku) | 9 de 9 con respuesta válida (una se perdió por JSON roto) | 1 de 10 |

Las dos rondas costaron unos 0,56 $; cada auditoría sale a unos 1,4 céntimos, más barata que la de la v1 (2,2). Comparé qué modelo audita mejor en estas pruebas: **no encontré diferencias que sobrevivan a un tamaño así** (10 por grupo: una diferencia de 1 o 2 casos es ruido; no conservo en el repo los números de esa comparación). La parte de código es la que hace el trabajo, y con esa estructura la elección del modelo importa mucho menos, porque su salida se puede comprobar.

Una de las «falsas alarmas» **no lo era**. En ADTN la tesis decía «$512K del CFO»; lo comprobé en la base y el CFO compró 99.323 $ en total. El Detective se había equivocado y el modelo, con los datos delante, lo pilló. En IBIO señaló «$600k+» frente a unos 223.000 $ reales, aunque ahí su cita no era literal y no cuenta oficialmente.

Sobre análisis reales la cosa fue menos limpia: con el comprobador de producción (7-nov), sobre los 85 análisis más recientes (el último de cada modelo en cada empresa), marcó alguna cifra en 18 (21 %), 22 cifras en total. Y no por igual: 16 de 32 análisis de Claude (50 %) frente a 2 de 53 de Gemini (4 %), porque Claude escribe muchas más cifras derivadas (sumas de compras de insiders, porcentajes de crecimiento). Aún tengo que revisar a mano cuántas son errores y cuántas cálculos legítimos.

**Qué decidí.** Integrarlo (vuelta 8).

**Virtud.** El modelo propone y el código comprueba: una cita inventada no cuenta, y una cifra marcada solo se absuelve si el modelo enseña un cálculo cuyos operandos están de verdad en el texto. Barato, y encontró un error real que nadie había visto.

**Coste.** Mi prueba sintética favorece al código: la cifra cambiada es justo el error que el código detecta, y los fallos reales de un Detective suelen ser más sutiles (una causa mal atribuida, un riesgo omitido). No mide lo que sí aportaría un modelo. En los hallazgos de contenido el modelo es ruidoso: en una ronda detectó que SSII decía tener la aprobación de la FDA cuando el texto decía que estaba en trámite, y en la siguiente ronda no. Y el 7 % de las afirmaciones que da por «respaldadas» llevaban una cita que no existía (el verificador las pilla, que es justo lo que el Auditor v1 no podía ver). También: Haiku rompía el JSON cuando la cita llevaba comillas dobles (1 de 20); lo resolví pidiéndole comillas simples y reintentando.

---

## 8. El Auditor v2 en producción (7 nov)

**Qué probé.** Llevar el prototipo a producción (`auditor.py`, `verificaciones.py`, `verificacion_cifras.py`), con una tabla propia (`verificaciones`) en vez de `auditorias`: allí un `veredicto` nulo es una fila de score que el scorer borra y reescribe, y uno no nulo es un análisis que cuentan el dashboard, la API y el orquestador. Cruzado (Claude audita a Gemini y viceversa) y **encendido por defecto**, con un interruptor (`auditor_activo`) para apagarlo sin tocar código. Había dejado el Auditor v1 apagado por defecto mientras decidía qué hacer con él, y lo reconsideré: la segunda mirada es parte de la idea del proyecto (no fiarse de un solo modelo), así que el v2 nace encendido.

**Qué esperaba.** Sellos creíbles: `VERIFICADO` o `CON_AVISOS`, con el detalle de qué falló.

**Qué pasó en la primera prueba real** (3 empresas, 6 sellos, unos 3,5 céntimos de Claude): **4 `VERIFICADO` y 2 `CON_AVISOS`**.

- ASPS: la tesis decía «proveedor único» frente a «one of only a few providers» en el texto. Aviso legítimo.
- BFS: la tesis decía «ocupación similar a Twinbrook» con una cita de 64,2 % que solo es de Hampden. Dudoso, **posible falsa alarma**: no sé si es un error del Detective o una pedantería del Auditor.

**Qué decidí.** Dejarlo encendido y medir con datos reales, sin presentar los 6 sellos como una evaluación.

**Virtud.** El Auditor pasó de dar una opinión ruidosa a dar un resultado que se puede comprobar: el sello dice cuánto fiarse de cada análisis sin cambiar el veredicto.

**Qué pasó después (la tarde del mismo día).** Verifiqué otras 15 empresas con el mismo Auditor (27 sellos más, sin avisos de Telegram para no saturarme, unos 15 céntimos de Claude): en total, **33 sellos en 18 empresas, 21 `VERIFICADO` y 12 `CON_AVISOS`**. Los avisos no se reparten igual: salen en 10 de 15 análisis de Claude y en 2 de 18 de Gemini, que casa con que el comprobador de cifras marque 16 de 32 análisis de Claude y 2 de 53 de Gemini (Claude escribe muchas más cifras derivadas). Y por tipo: 6 avisos son cifras y **14 son afirmaciones que el modelo da por contradichas** por el texto, con una cita real. Esa segunda clase es justo donde el modelo opina, y no he revisado a mano cuántas son errores del Detective y cuántas pedantería del Auditor.

**Coste.** Solo 33 sellos (y, tras la vuelta 12, 13 análisis por verificar en las 8 empresas que siguen siendo candidatas). Falta medir las falsas alarmas a escala con las 22 cifras que hoy marca el código, etiquetadas a mano. Y el sello **no mira las afirmaciones sin respaldo ni las citas falsas del propio Auditor**: de los 21 `VERIFICADO`, 9 tienen alguna afirmación sin respaldo y 7 alguna «respaldada» con una cita que no se verifica (en ASPS, un análisis tenía 3 de 6 sin respaldo y solo lo marcó una contradicción). Además el comprobador de cifras es permisivo: en los textos de tres empresas, entre 77 y 79 de los enteros del 5 al 100 aparecen en el documento, así que un porcentaje inventado suele pasar. Decisión abierta. Si el sello da avisos de más, perderá credibilidad.

---

## 9. El veredicto cambia al re-analizar

**Qué probé.** Mirar qué pasa cuando el mismo modelo analiza la misma empresa dos veces. Los cambios entre pasadas (`cambios.py`) lo hicieron visible: en 30 días hubo 18 cambios de veredicto.

**Qué esperaba.** Pequeñas variaciones.

**Qué pasó.** ADTN pasó, en los dos modelos a la vez, de `INTERESANTE` (14-oct, protocolo 1) a `MUY_INTERESANTE` (15-oct, protocolo 2) y de vuelta a `INTERESANTE` (3-nov). Mirando todo lo que hay en la base: de 49 pares de análisis consecutivos de la misma empresa por el mismo modelo, el veredicto cambió en **27 (55 %)**. Con el mismo protocolo en ambos (2 contra 2) fueron 4 de 8; con el protocolo 1 contra el 1, 12 de 21; del 1 al 2, 11 de 20.

**Qué decidí.** Aceptarlo y no maquillarlo: es la razón de tener dos votos y de enseñar el desacuerdo en vez de promediarlo.

**Virtud.** Saber que el veredicto de un solo análisis no es una medida estable, antes de que alguien (yo) se ponga a confiar en un `MUY_INTERESANTE` suelto.

**Coste.** Entre dos análisis pueden haber entrado datos nuevos (un Form 4, un 8-K) o haber cambiado el protocolo, y no separé esas causas del ruido del modelo. Con 8 pares del mismo protocolo, no hay muestra para decir cuánto es ruido puro. Y el sello del Auditor no arregla esto.

---

## 10. Robustez, copias y alertas (sep-nov)

**Qué probé.** Dejar el orquestador corriendo solo cada 48 h y ver cómo falla.

**Qué pasó, y qué hice.**

- Modelos retirados por un fallo suelto: ahora hace falta que fallen 3 seguidos (25-sep).
- El JSON de Claude salía cortado en el Auditor: el registro de tokens (`llm_calls`, 14-oct) mostró que `tokens_salida` era igual al máximo en todas las llamadas fallidas. No era el modelo: era `max_tokens` (2.000, luego 4.000 y hoy 8.000). Los 429 de cuota ya no agotan los reintentos.
- Errores clasificados por tipo (cuota, saturado, timeout, JSON roto, JSON cortado), alertas por Telegram cuando el sistema se degrada, y control de la cuota gratuita de Gemini (20 llamadas al día contadas desde `llm_calls`; la pasada se recorta a lo que cabe).
- Tiempos máximos por paso, un daemon que sobrevive a una excepción, Telegram escapado, una API que valida los modelos, lo ya pagado no se pierde si falla una llamada posterior y un veredicto ausente o inválido se descarta en vez de rellenarse con un `INTERESANTE` inventado.
- Mis scripts de ingesta se tragan los errores por empresa y terminan con código 0 aunque no traigan nada, así que no me fío de lo que digan: ahora se mide la edad del dato más nuevo de cada fuente y se avisa si pasa de un umbral (Form 4: 130 días porque llegan por trimestres; mercado: 7; 8-K: 21; 13D/G: 45; 10-Q: 140).
- **Copia de seguridad verificada** (`backup_db.py`): `pg_dump` más lectura de vuelta con `pg_restore --list`, rota las últimas 7. La restauré en una base temporal y los recuentos coincidían (filas de `auditorias` 404, transacciones de insiders 3.573.059, empresas 11.251, llamadas a modelos 273). Lo único irreemplazable es lo que está en Postgres: los análisis pagados.
- **Docker Compose** (base, API y dashboard). El orquestador se queda fuera a propósito.

**Virtud.** Cada una de estas cosas salió de un fallo real y medido, no de una lista de buenas prácticas.

**Coste.** Más código que mantener y más superficie que probar (330 tests). Y un fallo que solo vi al revisarlo todo antes de publicar: `backup_db.py` buscaba el contenedor `smallcap_hunter_db` (el del arranque manual), y con Docker Compose el contenedor se llama distinto, así que la copia automática no habría encontrado la base. Ahora el nombre se configura con `DB_CONTAINER`.

---

## 11. Fallos de datos y de concurrencia

Cosas que salieron del `git log` y que no eran de modelos:

- **Condición de carrera en el grafo de LangGraph** (21-sep). Los dos nodos Detective corren en paralelo y compartían una conexión de psycopg2, es decir, una transacción. Si un nodo fallaba y hacía `rollback`, borraba en silencio el `INSERT` todavía no confirmado del otro, que además ya había mandado el aviso de Telegram como si se hubiera guardado. Ahora cada nodo abre su propia conexión.
- **La SEC movió un archivo** (24-sep). El ZIP de Form 4 del segundo trimestre de 2026 cambió de ruta sin redirección y `loader_backfill.py` recibía un 404 y daba el trimestre por inexistente: en la base no había ninguna compra de insiders posterior al 31 de marzo. Ahora prueba las dos rutas.
- **Yahoo bloqueaba mi cliente** (24-sep). La versión antigua de yfinance recibía 429; actualicé a la 1.7.0, que imita a un navegador, y corté el lote ante una racha de rate-limit.
- **El cluster tenía que ser reciente** (24-sep). La Capa 1 y la 2 aceptaban un cluster de hace años: empresas con compras antiguas aparecían como candidatas de hoy. Al exigir recencia, salieron de la lista, entre otras, nuestra única `MUY_INTERESANTE` de entonces, ENOV.

**Virtud.** Los cuatro se encontraron porque comparé lo que el sistema decía con lo que había en los datos.

**Coste.** El de la carrera estuvo ahí, silencioso, hasta que lo encontré leyendo el código. El de la SEC pudo haber dejado meses sin compras nuevas sin que nadie se enterase: de ahí las alertas de ingesta parada.

---

## 12. Un fallo en el score de aceleración, encontrado al documentar el código (7 nov)

**Qué probé.** Documentar todo el código línea a línea para poder defender cada línea. Al llegar a `scorer_capa2.py`, un recorrido por `score_temporal` terminó en una pregunta incómoda: ¿de dónde sale la etiqueta de año y trimestre de cada dato?

**Qué esperaba.** Que dos trimestres con etiquetas consecutivas lo fueran de verdad. El score de aceleración solo debía calcular la tasa de crecimiento entre trimestres adyacentes.

**Qué pasó.** No era así. `anio_fiscal` y `trimestre` vienen del último informe que menciona cada periodo (en `enriquecedor_xbrl.py` gana la reexpresión más reciente, una decisión de julio pensada para los valores), y una cifra del año anterior que reaparece como comparativa queda con la etiqueta del año nuevo. En BFS, el cierre del 30-jun-2024 figuraba como 2025 Q2 y el del 30-sep-2025 como 2025 Q3: «consecutivos» con 15 meses de por medio. Midiendo la serie que ve el scorer en las 106 empresas puntuadas, 80 de 435 pares «consecutivos» distaban unos 455 días, en 78 de ellas.

Mi primera reacción tuvo un tropiezo, y lo cuento. Recordaba haber decidido tolerar huecos de unos 15 meses porque las small caps presentan tarde, y por eso al principio dudé de que fuera un fallo. Busqué en los archivos de julio y dicen lo contrario: la auditoría de entonces lista como problema que `score_temporal` empalma trimestres no consecutivos («con huecos grandes la aceleración medida es ficción»), y el test de julio exige saltarse esos pares. Lo que sí se decidió a propósito fue usar el año y trimestre fiscales de la SEC (por los años fiscales distintos); el efecto sobre las etiquetas no se había previsto. Y los retrasos al presentar no lo explican: la fecha de cierre del periodo no cambia aunque el informe llegue tarde.

**Qué decidí.** Decidir la adyacencia con la fecha de cierre (entre 70 y 115 días, para tolerar los calendarios de 52 y 53 semanas) y ordenar también las series de márgenes y de caja por esa fecha. No toco los datos de la base ni las etiquetas fiscales, solo cómo las lee el score. Antes de reescribir los scores hice una copia de seguridad verificada.

**Resultado.** El sub-score de aceleración cambia en 32 de las 106 empresas (más 3 por el orden de la serie de márgenes). Las candidatas (score ≥ 25) pasan de 27 a 17: 10 bajan y ninguna sube. En las 33 empresas con score distinto, 18 bajan de score y 6 suben, sin cruzar el 25.

**Virtud.** Un fallo del núcleo del proyecto encontrado por leer el código despacio, no por una métrica, y corregido con un test que falla de verdad si vuelve a ocurrir (lo comprobé deshaciendo el arreglo a propósito). El test que había solo exigía que el resultado fuera un entero: los 327 tests no lo habrían pillado.

**Coste.** Todo lo que medí antes sobre la lista de candidatas, incluida la primera instantánea de las tesis V1 y V2, viene del score antiguo y hay que repetirlo. Las 10 empresas que dejan de ser candidatas ya tienen análisis guardados (y algunas, sellos), pero no se analizarán más hasta que vuelvan a pasar el corte. Y la corrección responde a una pregunta (¿son consecutivos?) sin tocar otra más de fondo: si este score, tal como está diseñado, es una buena forma de elegir empresas. Eso es lo siguiente.

---

## 13. Etiquetar a mano lo que marca el Auditor (13 nov)

**Qué probé.** Etiquetar a mano 50 casos que el Auditor v2 ya había marcado: 26 cifras que el código no encontró en el texto y 24 afirmaciones que el modelo dio por contradichas. Para cada cifra, con los datos de la base delante (compras sumadas por persona, trimestres con su fecha), elegí entre «el Detective se equivocó», «es correcta, sale de calcular con los datos», «es correcta, está tal cual» o «no puedo decidirlo». Para cada afirmación: contradicción real, pedantería, el Auditor se equivocó, el Detective no dijo eso, o no sé.

**Qué esperaba.** Que una parte importante de las alarmas fueran falsas, como en el Auditor v1. No tenía una hipótesis más precisa.

**Qué pasó.**
- **Afirmaciones:** de 24, 15 eran contradicciones reales (62 %), 8 eran fallos del Auditor y 1 una pedantería. Varios de los 8 fallos del Auditor son sobre cifras y fechas (importe de las compras, periodo en que ocurrieron, posición frente a mínimos de 52 semanas).
- **Cifras:** de 26, 11 eran errores del Detective, 4 cálculos correctos y 11 no pude decidirlas. Entre las 15 decididas, 73 % eran errores reales. Ninguna estaba tal cual en los datos, así que el verificador no marcó cifras que estuvieran escritas.
- Los números pequeños pesan: con 24 y 15 casos, los intervalos aproximados son 43–79 % y 48–89 %.
- Al revisar las cifras encontré un fallo en el propio verificador: `$3.2M` y `$4M` no se comprobaban nunca (se leían como 3,2 y 4, y el filtro ignora lo menor de 5), y las marcadas se mostraban sin su K o M. Lo corregí con tests; no cambia la cifra del 73 %, que se midió antes.

**Qué decidí.** Contar la cifra con su límite: lo medido es la precisión de las alarmas, no cuánto se le escapa al sello. El siguiente paso es etiquetar 30 análisis completos, `VERIFICADO` incluidos.

**Virtud.** Por primera vez el sello tiene una cifra de acierto hecha contra juicio humano,.

**Coste.** Un solo etiquetador, casos elegidos entre los ya marcados, 11 cifras sin decidir (si yo no puedo decidirlas con los datos delante, el sello solo sirve si enseña de dónde sale cada cifra) y la mayoría de cifras son de Claude.

---

## Lo que todavía no sé

Cada pregunta tiene una forma de medirla.

1. **¿Cuántos errores se le escapan al sello?** La vuelta 13 midió cuántas de sus alarmas son reales (62 % de las afirmaciones, 73 % de las cifras decididas), pero no lo que deja pasar. Habría que etiquetar a mano análisis completos, `VERIFICADO` incluidos, y contar los errores que no avisó.
2. **¿Reduce errores el Auditor v2 en la práctica?** Habría que plantar errores en análisis nuevos o contrastar los avisos con una revisión humana. Hoy solo sé que detecta una cifra cambiada en un caso fácil.
3. **¿Qué parte de la inestabilidad del veredicto es ruido del modelo?** Re-analizar la misma empresa N veces con los mismos datos y el mismo protocolo, y contar cuánto cambia. Hoy tengo 8 pares del mismo protocolo, con datos posiblemente distintos.
4. **¿Debe el sello avisar por afirmaciones sin respaldo?** Mirando, sobre los análisis ya verificados, cuántas afirmaciones sin respaldo hay y cuántas eran errores reales.
5. **¿Aporta algo un modelo mayor como Detective?** Sonnet en unas decenas de empresas, contra Haiku, con la misma vara de citas con soporte y desacuerdo.
6. **¿Bate la señal al mercado cuando se resuelve el sesgo de supervivencia?** Con datos que conserven las empresas deslistadas, un grupo de control (empresas parecidas sin cluster) y el análisis guardado en el repo con su script.
7. **¿Hay diferencia entre la tesis V1 y la V2?** Hay que esperar: medianas de exceso a 12 y 24 meses frente al IWM, con intervalo por bootstrap. Con unas decenas de candidatas por brazo, solo se verían diferencias enormes.
8. **¿Discrimina el veredicto de los agentes?** Hoy el único contraste (57 casos ciegos, `INTERESANTE` igual que `NADA_INTERESANTE`) es del Detective antiguo. Habría que repetirlo con el protocolo 2 y con muestra más grande.
9. **¿Sirve el contexto RAG?** Solo 172 trimestres y 494 eventos tienen embedding. Habría que poblarlo y comparar análisis con y sin él.
10. **¿Anclan el score los veredictos?** En los 62 análisis del protocolo 2 no hay ningún `NADA_INTERESANTE`, con distribución idéntica en los dos modelos (42 / 20). Habría que ocultar el score en el prompt de una muestra y ver si el veredicto cambia.
11. **¿Importa que el Detective solo vea 12.000 caracteres del MD&A?** El texto medio tiene unos 50.000 y el verificador comprueba contra el texto completo. Habría que contar cuántas citas aceptadas caen fuera de la parte que el modelo vio, y probar con más contexto.
12. **¿Es buen diseño el propio score?** Los pesos (cuatro sub-scores de 0 a 10), qué señales cuentan y cómo se mide la aceleración no están contrastados con la literatura. Habría que revisar qué dice sobre compras de insiders, aceleración de ingresos y posición en el rango de 52 semanas, y comparar variantes sobre los mismos datos.
