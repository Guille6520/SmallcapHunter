# SmallCap Hunter

[![tests](https://github.com/Guille6520/SmallcapHunter/actions/workflows/tests.yml/badge.svg)](https://github.com/Guille6520/SmallcapHunter/actions/workflows/tests.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Un sistema de LLM que analiza small caps de EE. UU. con documentos de la SEC y no se fía de sí mismo.** Dos modelos analizan cada empresa a ciegas, un programa comprueba que cada cita existe en el documento original y un Auditor cruzado sella cada análisis. Lo que no funciona está medido y publicado.

[Las vueltas del proyecto, con sus fallos](docs/hallazgos.md) · [Límites](docs/limitaciones.md) · [Cómo arrancarlo](docs/arranque.md)

![Demostración del dashboard: candidatas con el voto de cada modelo, ficha con citas verificadas y sellos del Auditor](docs/demo.gif)

## Qué hace

1. **Datos públicos de la SEC**: compras de insiders (Form 4), métricas trimestrales (XBRL), 10-Q, 8-K y 13D/G.
2. **Filtros y un score de 0 a 40**, sin modelos: de 11.251 empresas quedan 16 candidatas.
3. **Dos «Detectives» a ciegas** (Claude Haiku 4.5 y Gemini 2.5 Flash, orquestados con LangGraph) escriben una tesis con citas literales. Ninguno ve lo que dice el otro.
4. **Un verificador que es código, no un modelo**, comprueba que cada cita está en el documento.
5. **Un Auditor cruzado** (cada modelo revisa al otro) contrasta cifras y afirmaciones y pone un sello: `VERIFICADO` o `CON_AVISOS`.

Hasta hoy: 187 análisis en 64 empresas, 348 llamadas a modelos y unos 3,7 $ de gasto total. Python, PostgreSQL con pgvector, LangGraph, FastAPI, Streamlit, Docker Compose y 330 tests que corren sin red ni claves.

## Lo que está medido, y lo que no

| Pregunta | Lo que sé hoy | Dónde |
|---|---|---|
| ¿Se inventan las citas los modelos? | Con el primer prompt, el verificador marcaba como alucinación 10 de 21 análisis de Claude y 20 de 51 de Gemini. Con el actual, 0 de 81, y el 95 % de las citas tiene soporte en el texto. Parte de la mejora es medir mejor, no mejores modelos. | [evaluacion-citas.md](docs/evaluacion-citas.md) |
| ¿Sirve un auditor que da su propio veredicto? | No. Mi primer Auditor declaró que el análisis empeoraba en 96 de 130 auditorías, y en 24 de 24 cuando revisaba los suyos. Era ruido. Lo rehíce para que verifique en vez de opinar. | [hallazgos.md, vueltas 4 y 8](docs/hallazgos.md) |
| ¿Coinciden los dos modelos? | En 18 de 36 empresas: lo mismo que el azar. Por eso enseño los dos votos y no una media. | [limitaciones.md](docs/limitaciones.md) |
| ¿Es fiable el sello del Auditor? | Aún no lo sé. Hay 50 sellos (29 verificados y 21 con avisos), y el resultado depende demasiado de qué modelo audita. Estoy etiquetando los avisos a mano para medir las falsas alarmas. | [limitaciones.md](docs/limitaciones.md) |
| ¿La selección bate al mercado? | **No hay evidencia de que lo haga.** Es una herramienta de investigación, no un consejo de inversión. | [hallazgos.md, vuelta 1](docs/hallazgos.md) |

Este proyecto no es un selector de acciones. Es un sistema de LLM donde cada afirmación se puede comprobar, y donde he medido lo que falla en vez de esconderlo. Lo construí como inversor, después de más de diez años en los mercados: quería una herramienta que leyera por mí los informes que no tengo tiempo de leer, y que me dijera cuánto fiarme de lo que cuenta.

## Por qué es distinto

- **Votos ciegos.** Dos modelos de familias distintas analizan la misma empresa por separado. El veredicto son sus dos votos, y el desacuerdo se ve.
- **Citas verificadas por código.** Una cita traducida, parafraseada o inventada no cuenta como respaldo. Lo decide un programa, sin otro modelo de por medio.
- **Un Auditor que verifica.** Una parte es código (busca cada cifra de la tesis en el texto) y otra es el modelo, acotado: solo puede absolver una cifra si enseña un cálculo cuyos operandos están en el documento.
- **Los fallos, documentados.** Cada cambio de diseño viene de una medición, incluidas las que salieron mal: [doce vueltas, con sus números](docs/hallazgos.md).

![Candidatas con el voto de cada Detective y su sello](docs/capturas/candidatas.png)

![Lo que encontró el Auditor en un análisis: una afirmación contradicha por el texto y riesgos omitidos, cada uno con su cita](docs/capturas/sellos.png)

## Arquitectura

![Arquitectura: de los datos de la SEC a la candidata con sus votos y sellos](docs/architecture.svg)

Alrededor del pipeline hay un orquestador que hace una pasada cada 48 horas y solo analiza empresas con datos nuevos, control de la cuota gratuita de Gemini, registro de coste y latencia de cada llamada, alertas por Telegram cuando algo se degrada y copias de seguridad que se verifican leyéndolas de vuelta. Detalle en [operacion.md](docs/operacion.md).

## Cómo arrancarlo

```bash
git clone https://github.com/Guille6520/SmallcapHunter.git
cd SmallcapHunter
cp .env.example .env        # rellena al menos DB_PASSWORD
docker compose up -d --build
```

Levanta Postgres con pgvector, la API (<http://localhost:8000/docs>) y el dashboard (<http://localhost:8501>). La base arranca **vacía**: para ver datos hay que ejecutar la ingesta y el orquestador, que se explican paso a paso en [arranque.md](docs/arranque.md). Las claves de Anthropic y Gemini solo hacen falta para analizar.

## Documentación

| Documento | Qué cuenta |
|---|---|
| [hallazgos.md](docs/hallazgos.md) | Las doce vueltas del proyecto: qué probé, qué esperaba, qué pasó y qué decidí |
| [limitaciones.md](docs/limitaciones.md) | Todo lo que no sé o no funciona, con sus cifras |
| [evaluacion-citas.md](docs/evaluacion-citas.md) | Cómo se midió si los modelos se inventan las citas |
| [arranque.md](docs/arranque.md) | Instalación, ingesta, pipeline, orquestador, dashboard y estructura del código |
| [operacion.md](docs/operacion.md) | Observabilidad, alertas, tests y CI |

## Lo siguiente

1. **Un conjunto de referencia etiquetado a mano**, para saber cuántos avisos del Auditor son errores reales. En curso.
2. **Experimentos de evaluación**: repetir análisis con los mismos datos, quitar el score del prompt, dar más texto a los modelos y plantar errores para medir al Auditor.
3. **Contrastar el score con la literatura** en un backtest con las hipótesis fijadas de antemano.
4. **Dar forma de paquete al código** y validar las respuestas de los modelos con esquemas.
5. **Una demo con datos de ejemplo** y versión en inglés.

## Licencia

MIT, ver [LICENSE](LICENSE).

> Proyecto de investigación sobre datos públicos de la SEC. No es un producto financiero ni un consejo de inversión.
