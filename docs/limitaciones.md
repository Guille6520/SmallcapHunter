# Lo que sé y lo que no

Esta es la parte que más me importa. Un proyecto que solo cuenta lo que le salió bien no sirve para decidir nada.

> **Actualización del 13-nov-2026.** Cifras nuevas, medidas en mi base, que matizan lo de abajo:
> - Hay 50 sellos del Auditor (29 `VERIFICADO` y 21 `CON_AVISOS`). El resultado depende de qué modelo audita: Gemini pone avisos en 18 de 23 análisis y Claude en 3 de 27. Como cada modelo solo audita al otro, no puedo separar si Gemini es más estricto o si los análisis de Claude son peores.
> - De los 29 `VERIFICADO`, 20 llevan alguna afirmación sin respaldo o alguna cita del Auditor que no se verifica.
> - Los dos Detectives coinciden en el veredicto en 18 de 36 empresas (50 %; kappa de Cohen de -0,14): no más que el azar. Y en 81 análisis con el protocolo actual no hay ningún `NADA_INTERESANTE`.
> - Las candidatas son 16 (score ≥ 25) tras corregir el score de aceleración.

**1. No he demostrado que la señal bata al mercado.** Lo he intentado medir de varias formas y ninguna lo respalda:

- *Validación histórica* (`descubrir_clusters_historicos.py`, `validacion_historica.py`). Busqué clusters de insiders en todo el histórico de mi base: 4.305 casos. Para saber si la empresa era de verdad una small cap *en ese momento* reconstruí su capitalización de la época (precio histórico por acciones en circulación). Solo pude validar **274** de los 4.305: 218 ya eran de otro tamaño, 415 no tienen precio histórico en Yahoo (deslistadas, sobre todo) y 3.398 no tienen dato de acciones en circulación para esa fecha. Es decir, la muestra es el 6 % de lo que encontré, y está sesgada hacia empresas que siguen cotizando.
- *Backtest de rendimiento frente al Russell 2000 (IWM).* Hice un análisis exploratorio con esos 274 eventos que apuntaba en la misma dirección (exceso de retorno mediano negativo a 12 y 24 meses y la mayoría de los eventos por debajo del índice), pero **no conservo ni el script ni los resultados, así que no publico cifras: no puedo reproducirlas.** `backtest_rendimiento.py` (que sí está) se hizo para esta pregunta, pero solo escribe su resumen en el log y no guarda resultados. Rehacerlo con resultados guardados está en el roadmap.
- *Recall sobre «ganadoras»* (`validacion_recall_ganadoras.py`, resultados en `resultados_test_ganadoras.csv`). De 141 empresas de mi base que multiplicaron por 3 o más desde su mínimo como small cap, 86 (61 %) habían mostrado el cluster antes. Es un número que impresiona y que no prueba nada: no hay grupo de control (cuántas empresas con cluster *no* subieron), las ganadoras se eligen sabiendo el resultado y solo veo las que siguen vivas.
- *Veredictos de los agentes contra el resultado* (`backtest_capa3.py`, resultados en `resultados_ciego_mixtos.csv`). Hice que el Detective analizara en ciego (empresa anonimizada y años enmascarados) 57 casos históricos, con una versión anterior del Detective. Dio 46 `NADA_INTERESANTE`, 10 `INTERESANTE` y 1 `MUY_INTERESANTE`. El exceso mediano a 12 meses fue de -18 % tanto en los `INTERESANTE` como en los `NADA_INTERESANTE`: el veredicto no distinguía. Y en una prueba aparte con 4 empresas que acabaron subiendo mucho (`resultados_ciego_ganadoras.csv`), el agente dio `NADA_INTERESANTE` a las cuatro.

Limitaciones de todo lo anterior: muestras pequeñas, sesgo de supervivencia (lo que deja de cotizar desaparece de los datos de precio, aunque la dirección neta del sesgo no está medida), mercado común para todas las empresas y eventos muy concentrados en 2022–2025 (comparten régimen). No tengo evidencia, ni a favor ni en contra, de que el sistema actual funcione como método de selección. Tampoco tengo evidencia de que lo contrario sea cierto: lo que sé es que los controles que he hecho no encuentran ventaja.

**2. La hipótesis de fondo es una pregunta abierta, y la trato como tal.** La Capa 2 premia estar cerca de mínimos de 52 semanas (tesis V1). En la rama [`tesis-v2`](https://github.com/Guille6520/SmallcapHunter/tree/tesis-v2) hay la tesis contraria (premiar máximos, apoyada en George y Hwang, 2004), **pre-registrada** antes de ver resultados: el hash del commit es su sello de fecha. Son dos tesis distintas, no una mejora medida de la otra. Hoy hay una sola instantánea (26 candidatas en V1, 11 en V2, 4 en las dos; es anterior a la corrección del score de aceleración y habrá que repetirla) y la muestra de cada brazo solo permitiría ver diferencias enormes. El resultado más probable a 12 y 24 meses es «no concluyente». Los resultados por empresa no se publican.

**3. La calidad del análisis sí la he medido, y es lo que funciona mejor.**

| Citas del Detective | Protocolo 1 (antes) | Protocolo 2 (ahora) |
|---|---|---|
| Análisis marcados `ALUCINACION`, Claude | 10 de 21 (48 %) | 0 de 31 |
| Análisis marcados `ALUCINACION`, Gemini | 20 de 51 (39 %) | 0 de 31 |
| Citas copiadas literalmente, Claude / Gemini | 47 % / 50 % | 56 % / 65 % |
| Citas con soporte en el texto, Claude / Gemini | no se medía | 96 % / 96 % |

Cómo leerlo sin venderlo mejor de lo que es: la caída de `ALUCINACION` no es solo que los modelos inventen menos. Cuando volví a puntuar con el verificador nuevo las respuestas del prompt antiguo, sin llamar a ningún modelo, las marcas de Claude bajaron de 6 de 10 a 1 de 10 y las de Gemini se quedaron en 2 de 7. En Claude casi todo el salto es medir mejor; en Gemini, el cambio de prompt. La única cifra comparable sin truco es la de citas literales. Las muestras son pequeñas y no están emparejadas. Y el verificador comprueba que la evidencia **existe**, no que la interpretación sea correcta: una cita real con una explicación inventada pasa. Método y consultas en [docs/evaluacion-citas.md](evaluacion-citas.md).

**4. Lo que no sé del Auditor v2.**

- **Lo que medí a mano el 13-nov-2026** ([vuelta 13](hallazgos.md)). De 24 afirmaciones que el Auditor declaró contradichas, 15 eran errores reales del Detective (62 %; intervalo aproximado 43–79 %), 8 eran fallos del Auditor y 1 una pedantería. De 26 cifras que marcó el código, pude decidir 15: 11 eran errores del Detective (73 %; 48–89 %) y 4 eran cálculos correctos. Las otras 11 no las pude decidir ni con los datos delante.
- Lo que esa medición **no** dice: solo etiqueté casos que el sistema ya había marcado, así que mide cuántas alarmas son reales, no cuántos errores se le escapan al sello. Es un solo etiquetador, sin segundo juicio. 24 de las 26 cifras eran de Claude y solo 3 de las 24 afirmaciones las auditó Claude, así que no puedo comparar a los dos modelos.
- Mi prueba sintética (20 análisis, con una cifra alterada a mano en 10) salió bien: el código detecta 10 de 10, con 1–2 falsas alarmas de 10. Pero es el caso fácil. Sobre los 85 análisis reales más recientes (el último de cada modelo en cada empresa), el comprobador de cifras marca alguna en 18 (21 %), 22 cifras en total, y no por igual: 16 de 32 análisis de Claude (50 %) frente a 2 de 53 de Gemini (4 %), porque Claude escribe muchas más cifras derivadas (sumas, porcentajes de crecimiento). Aún tengo que revisar a mano cuántas de esas 22 son errores y cuántas cálculos legítimos.
- El sello hoy **no mira las afirmaciones sin respaldo ni las citas falsas del propio Auditor**. Con los 33 sellos actuales: de los 21 `VERIFICADO`, 9 tienen alguna afirmación sin respaldo en el texto y 7 alguna afirmación que el Auditor da por «respaldada» con una cita que no se verifica. Es decir, `VERIFICADO` hoy significa «sin cifras dudosas ni contradicciones», no «todo lo que dice el análisis está respaldado». Es una decisión abierta.
- El comprobador de cifras es permisivo. En los textos de tres empresas, entre 77 y 79 de los 96 enteros del 5 al 100 aparecen en el documento, así que un porcentaje inventado suele pasar, y ver una cifra en el texto no prueba que signifique lo mismo. Tampoco hace la aritmética de un cálculo derivado: solo exige que dos operandos estén en el texto.
- Aún no sé si el Auditor v2 reduce errores en la práctica, ni si una cifra o afirmación marcada era un error o una pedantería.

**5. Otras limitaciones.**

- Unas 55 empresas analizadas son una muestra pequeña para sacar conclusiones.
- La cuota gratuita de Gemini (20 llamadas al día) limita a unas 6–10 empresas al día con el Auditor encendido.
- El veredicto no es estable: al re-analizar la misma empresa con el mismo modelo, el veredicto cambió en 27 de 49 re-análisis (el 55 %). Parte puede deberse a datos nuevos o al cambio de protocolo, no solo a ruido del modelo, y no he separado las dos causas.
- Los dos Detectives ven el mismo texto: «ciegos» quiere decir que no ven el análisis del otro, no que sean independientes entre sí.
- El RAG está poco poblado: solo 172 de 67.495 trimestres y 494 de 723 eventos 8-K tienen embedding, y el Auditor no ve el contexto RAG. Sin embeddings el Detective funciona igual, sin ese bloque. Además, la similitud del vecino más cercano es alta para casi todo (mediana 0,87, entre 0,81 y 0,93, medida el 7-nov) y no hay umbral mínimo: siempre se pegan 3 casos. Hoy aporta poco.
- Los veredictos casi no discriminan: en los 62 análisis del protocolo 2 no hay ningún `NADA_INTERESANTE` (42 `INTERESANTE` y 20 `MUY_INTERESANTE`, con la misma distribución en Claude y en Gemini). Sospecho anclaje por el score que ven en el prompt y por el corte de 25, pero no lo he medido.
- El Detective solo ve los primeros 12.000 caracteres del MD&A de cada 10-Q (el texto medio tiene unos 50.000; 317 de 319 lo superan), mientras que el verificador comprueba las citas contra el texto completo: puede dar por buena una cita de un trozo que el modelo no vio.
- La API no tiene autenticación ni límite de uso, y `POST .../analizar` gasta las claves de los modelos: está pensada para local.
- Con Docker Compose la base arranca vacía y el orquestador queda fuera. Además, `backup_db.py` busca por defecto el contenedor `smallcap_hunter_db` (el del arranque manual): con Compose el contenedor se llama distinto (míralo con `docker compose ps`) y hay que ponerlo en `DB_CONTAINER` del `.env`.
- Los precios vienen de yfinance, que no es una fuente oficial.
