"""
tests/test_nemotron.py — el proveedor opcional Nemotron (API de NVIDIA).

Sin red ni claves: la petición HTTP se sustituye por una falsa. Se comprueba lo que
importa de un proveedor que está apagado por defecto: que, si se pide a mano, manda lo
que debe (clave en la cabecera, razonamiento apagado), devuelve una RespuestaLLM con
tokens y motivo de fin, quita la valla ```json, convierte los errores HTTP en categorías
que el orquestador ya sabe contar, y no filtra la clave en ningún mensaje de error.
"""

import io
import json
import urllib.error

import pytest

import detective
from detective import RespuestaLLM, llamar_modelo, llamar_nemotron
from errores_llm import clasificar_error, describir_error

CLAVE = "nvapi-clave-falsa-para-tests"


class _Respuesta:
    def __init__(self, cuerpo):
        self._f = io.BytesIO(json.dumps(cuerpo).encode())

    def read(self, *a):
        return self._f.read(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _respuesta_api(contenido, finish="stop"):
    return {"choices": [{"message": {"content": contenido}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34}}


@pytest.fixture
def entorno(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", CLAVE)
    monkeypatch.delenv("NVIDIA_MODEL", raising=False)


def test_manda_la_clave_en_la_cabecera_y_apaga_el_razonamiento(monkeypatch, entorno):
    enviado = {}

    def falso_urlopen(peticion, timeout=None):
        enviado["cabeceras"] = dict(peticion.header_items())
        enviado["cuerpo"] = json.loads(peticion.data)
        return _Respuesta(_respuesta_api('{"ok": true}'))

    monkeypatch.setattr("urllib.request.urlopen", falso_urlopen)

    r = llamar_nemotron("hola")

    assert enviado["cabeceras"]["Authorization"] == f"Bearer {CLAVE}"
    assert enviado["cuerpo"]["model"] == detective.NVIDIA_MODELO_POR_DEFECTO
    assert enviado["cuerpo"]["temperature"] == 0
    assert enviado["cuerpo"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert isinstance(r, RespuestaLLM) and r == '{"ok": true}'
    assert (r.tokens_entrada, r.tokens_salida, r.motivo_fin) == (12, 34, "stop")


def test_el_modelo_se_puede_cambiar_por_entorno(monkeypatch, entorno):
    monkeypatch.setenv("NVIDIA_MODEL", "nvidia/otro-modelo")
    enviado = {}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda p, timeout=None: (enviado.update(m=json.loads(p.data)["model"]), _Respuesta(_respuesta_api("{}")))[1])

    llamar_nemotron("x")

    assert enviado["m"] == "nvidia/otro-modelo"


def test_quita_la_valla_json(monkeypatch, entorno):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda p, timeout=None: _Respuesta(_respuesta_api('```json\n{"a": 1}\n```')))

    assert llamar_nemotron("x") == '{"a": 1}'


def test_un_corte_por_el_tope_de_tokens_se_distingue(monkeypatch, entorno):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda p, timeout=None: _Respuesta(_respuesta_api('{"a": "cort', finish="length")))

    r = llamar_nemotron("x")

    assert r.motivo_fin == "length"


@pytest.mark.parametrize("codigo,categoria", [(429, "cuota_429"), (503, "sobrecarga_503"), (401, "autenticacion"),
                                                (400, "peticion_invalida")])
def test_los_errores_http_caen_en_las_categorias_que_ya_cuenta_el_orquestador(monkeypatch, entorno, codigo, categoria):
    def falla(peticion, timeout=None):
        raise urllib.error.HTTPError(peticion.full_url, codigo, "x", {}, io.BytesIO(b'{"detail": "mensaje del servidor"}'))

    monkeypatch.setattr("urllib.request.urlopen", falla)

    with pytest.raises(RuntimeError) as exc:
        llamar_nemotron("x")

    assert clasificar_error(str(exc.value)) == categoria
    assert describir_error(exc.value).startswith(categoria)


def test_la_clave_no_aparece_en_los_mensajes_de_error(monkeypatch, entorno):
    def falla(peticion, timeout=None):
        raise urllib.error.HTTPError(peticion.full_url, 401, "x", {}, io.BytesIO(b'{"detail": "bad key"}'))

    monkeypatch.setattr("urllib.request.urlopen", falla)

    with pytest.raises(RuntimeError) as exc:
        llamar_nemotron("x")

    assert CLAVE not in str(exc.value) and CLAVE not in repr(exc.value)


def test_el_despacho_lo_conoce_pero_no_se_usa_por_defecto(monkeypatch, entorno):
    monkeypatch.setattr(detective, "llamar_nemotron", lambda prompt: RespuestaLLM("del nemotron"))

    assert llamar_modelo("nemotron", "x") == "del nemotron"
    # Apagado por defecto: ni el cruce del Auditor ni los votos del Detective lo incluyen.
    import auditor
    assert "nemotron" not in auditor.CRUCE_MODELOS and "nemotron" not in auditor.CRUCE_MODELOS.values()


def test_el_coste_de_nemotron_es_cero_en_el_plan_de_prueba():
    assert detective.PRECIOS_USD_POR_TOKEN["nemotron"] == {"entrada": 0.0, "salida": 0.0}
