"""
errores_llm.py — categorías de error de las llamadas a los LLM.

Módulo ligero (solo la librería estándar) para que lo puedan usar el
orquestador y las alertas sin importar los SDK de Claude y Gemini, que
cuelgan de detective.py. detective.py lo reexporta.
"""

import re

# Categorías de error de una llamada a un LLM. Se guardan como prefijo de
# llm_calls.tipo_error ("cuota_429: You exceeded..."), así no hace falta una
# columna nueva ni tocar el esquema. Antes todo era texto libre, y un JSON
# cortado por el tope de tokens se veía igual que uno mal formado: tardamos
# días en descubrir que era el límite de salida y no el modelo.
CATEGORIAS_ERROR = (
    "truncado",           # el modelo paró por max_tokens: JSON cortado
    "json_invalido",      # JSON roto sin que haya sido el tope de tokens
    "cuota_429",          # límite de peticiones o cuota diaria agotada
    "sobrecarga_503",     # proveedor saturado o caído (503, 504, 529 overloaded)
    "timeout",
    "autenticacion",      # clave inválida o sin permisos (401/403)
    "peticion_invalida",  # 400: algo de la petición que el proveedor rechaza
    "error_proveedor",    # cualquier otro fallo de la API
)


def clasificar_error(mensaje: str) -> str:
    """Categoría de un error a partir de su texto. Si el texto ya empieza por
    una categoría (filas nuevas) la respeta; si no (filas anteriores a la
    clasificación) la deduce de lo que dice el proveedor."""
    texto = (mensaje or "").strip().lower()
    for categoria in CATEGORIAS_ERROR:
        if texto.startswith(categoria):
            return categoria
    if re.search(r"\b429\b|resource_exhausted|rate[_ ]limit", texto):
        return "cuota_429"
    if re.search(r"\b50[34]\b|\b529\b|unavailable|overloaded", texto):
        return "sobrecarga_503"
    if "timeout" in texto or "timed out" in texto:
        return "timeout"
    if re.search(r"\b40[13]\b|authentication|permission|api[_ ]key", texto):
        return "autenticacion"
    if re.search(r"\b400\b|invalid_request", texto):
        return "peticion_invalida"
    return "error_proveedor"


def describir_error(e: Exception) -> str:
    """Lo que se guarda en llm_calls.tipo_error para una excepción de la API."""
    texto = str(e)
    return f"{clasificar_error(texto)}: {texto}"[:200]


def describir_error_json(respuesta, e: Exception) -> str:
    """Para un JSON que no se pudo parsear: truncado si el proveedor dice que
    el modelo paró por el tope de tokens, json_invalido si no."""
    categoria = "truncado" if getattr(respuesta, "truncada", False) else "json_invalido"
    return f"{categoria}: {e}"[:200]
