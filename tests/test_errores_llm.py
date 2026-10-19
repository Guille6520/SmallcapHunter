"""
tests/test_errores_llm.py — clasificación de los errores de las llamadas a
un LLM. Nace de un caso real: el JSON cortado por el tope de max_tokens se
guardaba como "json_invalido", igual que uno mal formado, y tardamos días
en ver que no era el modelo sino el límite de salida. Sin red ni BD.
"""

import json
import sys
import types

import pytest

from detective import (
    CATEGORIAS_ERROR, RespuestaLLM, clasificar_error, describir_error,
    describir_error_json, llamar_claude,
)


@pytest.mark.parametrize("mensaje, categoria", [
    ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}", "cuota_429"),
    ("Error code: 429 - rate_limit_error", "cuota_429"),
    ("503 UNAVAILABLE. This model is currently experiencing high demand", "sobrecarga_503"),
    ("Error code: 529 - overloaded_error", "sobrecarga_503"),
    ("Request timed out.", "timeout"),
    ("Error code: 401 - authentication_error", "autenticacion"),
    ("Error code: 400 - {'error': {'type': 'invalid_request_error'}}", "peticion_invalida"),
    ("algo que no reconozco", "error_proveedor"),
    ("", "error_proveedor"),
])
def test_clasifica_errores_de_la_api_por_su_texto(mensaje, categoria):
    assert clasificar_error(mensaje) == categoria


def test_respeta_la_categoria_si_el_texto_ya_la_trae():
    """Las filas nuevas empiezan por su categoría; al releerlas no se
    reclasifican por las palabras sueltas que lleve el detalle."""
    assert clasificar_error("truncado: Unterminated string starting at: line 89") == "truncado"
    assert clasificar_error("json_invalido: Expecting ',' delimiter (char 429)") == "json_invalido", \
        "el 429 de un número de carácter no es una cuota"
    assert all(clasificar_error(f"{c}: lo que sea") == c for c in CATEGORIAS_ERROR)


def test_el_numero_suelto_dentro_de_otro_no_cuenta_como_codigo_http():
    assert clasificar_error("Error: request id req_4290x") == "error_proveedor"


def test_describir_error_guarda_categoria_y_detalle_acotado():
    texto = describir_error(RuntimeError("429 RESOURCE_EXHAUSTED " + "x" * 500))
    assert texto.startswith("cuota_429: 429 RESOURCE_EXHAUSTED")
    assert len(texto) <= 200


def _json_roto():
    try:
        json.loads('{"a": "cortado')
    except json.JSONDecodeError as e:
        return e


def test_json_cortado_por_el_tope_se_distingue_del_json_roto():
    e = _json_roto()
    assert describir_error_json(RespuestaLLM("x", motivo_fin="max_tokens"), e).startswith("truncado:")
    assert describir_error_json(RespuestaLLM("x", motivo_fin="MAX_TOKENS"), e).startswith("truncado:"), \
        "Gemini lo escribe en mayúsculas"
    assert describir_error_json(RespuestaLLM("x", motivo_fin="end_turn"), e).startswith("json_invalido:")
    assert describir_error_json("texto plano sin atributos", e).startswith("json_invalido:")


def test_respuesta_llm_sigue_siendo_un_str():
    r = RespuestaLLM("hola", tokens_entrada=1, tokens_salida=2, motivo_fin="max_tokens")
    assert r == "hola" and r.truncada and r.tokens_salida == 2
    assert not RespuestaLLM("hola").truncada


def test_llamar_claude_propaga_el_stop_reason(monkeypatch):
    """El motivo de fin sale de la respuesta real del proveedor: si Claude
    dice max_tokens, la RespuestaLLM lo lleva hasta quien clasifica el error."""
    respuesta = types.SimpleNamespace(
        content=[types.SimpleNamespace(text='{"a": "cortad')],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=8000),
        stop_reason="max_tokens",
    )

    class _ClienteFalso:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=lambda **k: respuesta)

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_ClienteFalso))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "clave-falsa")

    r = llamar_claude("prompt")

    assert r.truncada and r.tokens_salida == 8000
