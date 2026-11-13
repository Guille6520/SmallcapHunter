# Operación: observabilidad, alertas, tests y CI

## Observabilidad y alertas

Cada llamada a un modelo (Detective, Auditor, chat y resumen de negocio) se registra en `llm_calls` con tokens reales, latencia, coste estimado y el tipo de error si lo hubo. De ahí sale la pestaña Observabilidad: coste por modelo y por empresa, latencia, cuota de Gemini, errores por tipo (cuota, proveedor saturado, timeout, JSON roto o cortado por `max_tokens`) y calidad de citas por modelo y protocolo. Gracias a ese registro vi que Claude cortaba el JSON justo en el tope de tokens, no por fallo del modelo.

`alertas.py` revisa la salud del sistema al final de cada pasada y, solo si hay algo, avisa por Telegram: modelo con más del 25 % de llamadas fallidas, respuestas truncadas, cuota de Gemini casi agotada, gasto diario por encima de 1 $, calidad de citas baja, paso fallido o SEC sin contacto configurado, y **ingesta parada** (más de 130 días sin Form 4 nuevos, 7 sin mercado, 21 sin 8-K, 45 sin 13D/G o 140 sin 10-Q). Esto último porque mis scripts de ingesta se tragan los errores por empresa y terminan con código 0 aunque no traigan nada.

## Tests y CI

```bash
pip install -r requirements.txt
pytest
```

330 tests, **todos offline**: sin base de datos, sin claves, sin red. Cubren la lógica pura (normalización, filtros, score), el verificador de citas, el comprobador de cifras y los sellos, las reglas de votos e interruptores, los cambios entre pasadas, la copia de seguridad, las alertas, la cuota de Gemini, la política de retirada de modelos del orquestador, el grafo de LangGraph, el RAG y su degradación, y los endpoints de la API. GitHub Actions los ejecuta en cada push (`.github/workflows/tests.yml`, Python 3.12).

Qué cubren de verdad (cobertura de líneas medida con `pytest-cov`, 56 % del total): el núcleo está muy cubierto (verificador de citas 98 %, sellos y votos 100 %, API 94 %, orquestador 87 %, Auditor 80 %); las ingestas, poco (entre el 21 y el 31 %); el dashboard, casi nada (12 %: lo probé a mano y con una prueba de humo); y los scripts de validación histórica, nada, porque no son parte del pipeline. Más tests no es mejor por sí mismo: revisé la suite y no encontré redundancia (solo un par de tests con cuerpos casi iguales, y prueban escenarios distintos). Lo que falta es probar las ingestas contra respuestas de la SEC guardadas.

Los scripts de validación histórica no están en `tests/` a propósito: tardan 30–60 minutos, necesitan red y Postgres, y miden otra cosa (si la hipótesis se sostiene), no si el código hace lo que dice.
