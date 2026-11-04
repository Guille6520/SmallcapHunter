"""
tests/test_robustez_agentes.py — que el Detective y el Auditor no pierdan en
silencio lo que ya se ha pagado ni inventen lo que el modelo no dijo:

  - un veredicto ausente o inválido no se sustituye por INTERESANTE: el análisis
    se descarta y cuenta como fallo;
  - guardar un análisis devuelve si quedó guardado, y un fallo del aviso de
    Telegram no lo deshace ni lo cuenta como no guardado;
  - si una auditoría falla, las demás (y lo ya obtenido) se conservan;
  - el grafo no da por hecho un análisis que no se guardó.

La base de datos, los modelos y Telegram van simulados.
"""

import json

import pytest

import auditor
import detective
import grafo_capa3
from errores_llm import describir_veredicto_invalido, normalizar_veredicto


# ---------- veredicto ----------

@pytest.mark.parametrize("valor, esperado", [
    ("MUY_INTERESANTE", "MUY_INTERESANTE"), ("interesante", "INTERESANTE"),
    (" Nada_Interesante ", "NADA_INTERESANTE"), ("muy interesante", "MUY_INTERESANTE"),
    ("muy-interesante", "MUY_INTERESANTE"),
    (None, None), ("", None), ("   ", None), ("ALUCINACION", None), ("SEGURO", None),
    ("INTERESANTE!", None), (3, None), (["INTERESANTE"], None),
])
def test_normalizar_veredicto(valor, esperado):
    assert normalizar_veredicto(valor) == esperado


def test_el_error_de_veredicto_invalido_lleva_su_categoria():
    from errores_llm import clasificar_error
    texto = describir_veredicto_invalido(None)
    assert clasificar_error(texto) == "veredicto_invalido"


# ---------- Detective: veredicto inválido ----------

def _contexto():
    return {"empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc", "texto_mda": "texto del mda",
            "transacciones": []}


def _preparar_detective(monkeypatch, respuesta_modelo: str):
    llamadas = []
    monkeypatch.setattr(detective, "obtener_contexto_empresa", lambda conn, t: _contexto())
    monkeypatch.setattr(detective, "buscar_casos_similares_rag", lambda *a, **k: [])
    monkeypatch.setattr(detective, "formatear_casos_similares_rag", lambda c: "")
    monkeypatch.setattr(detective, "construir_prompt", lambda ctx, rag: "prompt")
    monkeypatch.setattr(detective, "llamar_modelo", lambda modelo, prompt: respuesta_modelo)
    monkeypatch.setattr(detective, "texto_fuente_citas", lambda ctx: "texto del mda")
    monkeypatch.setattr(detective, "registrar_llamada_llm", lambda **kw: llamadas.append(kw))
    return llamadas


def test_detective_sin_veredicto_se_descarta_y_cuenta_como_fallo(monkeypatch):
    llamadas = _preparar_detective(monkeypatch, json.dumps({"tesis": "algo", "citas": []}))

    assert detective.ejecutar_detective(object(), "FAKE", "claude") is None

    assert len(llamadas) == 1 and llamadas[0]["exito"] is False
    assert llamadas[0]["tipo_error"].startswith("veredicto_invalido")


def test_detective_con_veredicto_raro_tambien_se_descarta(monkeypatch):
    llamadas = _preparar_detective(monkeypatch, json.dumps({"veredicto_preliminar": "TAL VEZ", "citas": []}))

    assert detective.ejecutar_detective(object(), "FAKE", "gemini") is None
    assert llamadas[0]["exito"] is False


def test_detective_con_veredicto_valido_lo_normaliza_y_registra_exito(monkeypatch):
    llamadas = _preparar_detective(monkeypatch, json.dumps({"veredicto_preliminar": "muy interesante", "citas": []}))

    resultado = detective.ejecutar_detective(object(), "FAKE", "claude")

    assert resultado["respuesta_llm"]["veredicto_preliminar"] == "MUY_INTERESANTE"
    assert llamadas[0]["exito"] is True


# ---------- Detective: guardar ----------

class _Cur:
    def __init__(self, falla=False):
        self.falla = falla
        self.sql = []

    def execute(self, sql, params=None):
        if self.falla:
            raise RuntimeError("la base de datos se cayó")
        self.sql.append((sql, params))

    def close(self):
        pass


class _Conn:
    def __init__(self, falla=False):
        self.cur = _Cur(falla)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _resultado_detective(veredicto="INTERESANTE", alucinacion=False):
    return {
        "contexto": {"ticker": "FAKE", "nombre": "Fake Inc"},
        "respuesta_llm": {"veredicto_preliminar": veredicto},
        "verificacion_citas": {"alucinacion_detectada": alucinacion, "citas_soportadas": 3, "citas_totales": 3},
    }


def test_guardar_detective_devuelve_true_y_hace_commit(monkeypatch):
    monkeypatch.setattr(detective, "notificar_analisis", lambda **kw: None)
    conn = _Conn()

    assert detective.guardar_resultado(conn, 7, "claude", _resultado_detective()) is True
    assert conn.commits == 1 and len(conn.cur.sql) == 1


def test_guardar_detective_devuelve_false_si_la_base_de_datos_falla(monkeypatch):
    avisos = []
    monkeypatch.setattr(detective, "notificar_analisis", lambda **kw: avisos.append(kw))
    conn = _Conn(falla=True)

    assert detective.guardar_resultado(conn, 7, "claude", _resultado_detective()) is False
    assert conn.rollbacks == 1 and avisos == [], "no se avisa por Telegram de lo que no se guardó"


def test_un_fallo_de_telegram_no_deshace_ni_invalida_lo_guardado(monkeypatch):
    def telegram_caido(**kw):
        raise RuntimeError("Telegram caído")

    monkeypatch.setattr(detective, "notificar_analisis", telegram_caido)
    conn = _Conn()

    assert detective.guardar_resultado(conn, 7, "claude", _resultado_detective()) is True
    assert conn.commits == 1 and conn.rollbacks == 0


def test_guardar_detective_no_inventa_un_veredicto_ausente(monkeypatch):
    monkeypatch.setattr(detective, "notificar_analisis", lambda **kw: None)
    conn = _Conn()

    assert detective.guardar_resultado(conn, 7, "claude", _resultado_detective(veredicto=None)) is False
    assert conn.cur.sql == [], "no debe llegar a insertar nada"


def test_guardar_detective_marca_alucinacion_si_el_verificador_la_detecta(monkeypatch):
    monkeypatch.setattr(detective, "notificar_analisis", lambda **kw: None)
    conn = _Conn()

    detective.guardar_resultado(conn, 7, "claude", _resultado_detective(alucinacion=True))

    assert conn.cur.sql[0][1][1] == "ALUCINACION"


# ---------- Auditor ----------

def _analisis(id_, modelo):
    return (id_, modelo, {"veredicto_preliminar": "INTERESANTE"}, None)


def _preparar_auditor(monkeypatch, analisis, respuestas):
    """respuestas: por cada llamada al modelo, un texto JSON o una excepción."""
    registradas = []
    cola = list(respuestas)

    def llamar(modelo, prompt):
        r = cola.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(auditor, "obtener_contexto_empresa", lambda conn, t: {
        "empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc", "transacciones": []})
    monkeypatch.setattr(auditor, "obtener_analisis_detective", lambda conn, eid, m=None: analisis)
    monkeypatch.setattr(auditor, "ya_auditado_por", lambda conn, did, m: False)
    monkeypatch.setattr(auditor, "construir_prompt_auditor", lambda ctx, resp: "prompt")
    monkeypatch.setattr(auditor, "llamar_modelo", llamar)
    monkeypatch.setattr(auditor, "texto_fuente_citas", lambda ctx: "texto")
    monkeypatch.setattr(auditor, "verificar_citas", lambda *a, **k: {
        "alucinacion_detectada": False, "citas_soportadas": 1, "citas_totales": 1})
    monkeypatch.setattr(auditor, "registrar_llamada_llm", lambda **kw: registradas.append(kw))
    return registradas


def _auditoria(veredicto="EMPEORA_X"):
    return json.dumps({"veredicto_auditor": "NADA_INTERESANTE", "relacion_con_veredicto_detective": "EMPEORA",
                       "citas_nuevas": []})


def test_si_la_segunda_auditoria_falla_se_conserva_la_primera(monkeypatch):
    registradas = _preparar_auditor(
        monkeypatch, [_analisis(1, "claude"), _analisis(2, "gemini")],
        [_auditoria(), RuntimeError("429 RESOURCE_EXHAUSTED")],
    )

    resultados = auditor.ejecutar_auditor(object(), "FAKE")

    assert len(resultados) == 1, "la auditoría ya pagada no se pierde porque la siguiente falle"
    assert resultados[0]["respuesta_llm"]["detective_auditoria_id"] == 1
    assert [r["exito"] for r in registradas] == [True, False]


def test_si_la_primera_auditoria_falla_la_segunda_se_hace_igual(monkeypatch):
    _preparar_auditor(
        monkeypatch, [_analisis(1, "claude"), _analisis(2, "gemini")],
        [RuntimeError("503 UNAVAILABLE"), _auditoria()],
    )

    resultados = auditor.ejecutar_auditor(object(), "FAKE")

    assert len(resultados) == 1 and resultados[0]["respuesta_llm"]["detective_auditoria_id"] == 2


def test_auditoria_sin_veredicto_valido_se_descarta_sin_inventar_uno(monkeypatch):
    registradas = _preparar_auditor(
        monkeypatch, [_analisis(1, "claude")],
        [json.dumps({"relacion_con_veredicto_detective": "CONFIRMA", "citas_nuevas": []})],
    )

    assert auditor.ejecutar_auditor(object(), "FAKE") == []
    assert registradas[0]["exito"] is False and registradas[0]["tipo_error"].startswith("veredicto_invalido")


def test_guardar_auditor_devuelve_false_si_falla_y_true_si_no(monkeypatch):
    avisos = []
    monkeypatch.setattr(auditor, "notificar_analisis", lambda **kw: avisos.append(kw))
    resultado = {
        "empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc", "modelo_auditor": "gemini",
        "veredicto_final": "INTERESANTE", "respuesta_llm": {"relacion_con_veredicto_detective": "CONFIRMA"},
        "verificacion_citas": {"citas_soportadas": 1, "citas_totales": 1},
    }

    assert auditor.guardar_resultado_auditor(_Conn(falla=True), resultado) is False
    assert avisos == []
    assert auditor.guardar_resultado_auditor(_Conn(), resultado) is True
    assert len(avisos) == 1


def test_un_fallo_de_telegram_no_invalida_la_auditoria_guardada(monkeypatch):
    def telegram_caido(**kw):
        raise RuntimeError("Telegram caído")

    monkeypatch.setattr(auditor, "notificar_analisis", telegram_caido)
    resultado = {
        "empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc", "modelo_auditor": "gemini",
        "veredicto_final": "INTERESANTE", "respuesta_llm": {}, "verificacion_citas": {"citas_soportadas": 0, "citas_totales": 0},
    }
    conn = _Conn()

    assert auditor.guardar_resultado_auditor(conn, resultado) is True
    assert conn.commits == 1 and conn.rollbacks == 0


# ---------- el grafo ----------

def test_el_grafo_no_da_por_hecho_un_detective_que_no_se_guardo(monkeypatch):
    monkeypatch.setattr(detective, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(detective, "ejecutar_detective", lambda conn, t, m: _resultado_detective() | {
        "contexto": {"empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc"}})
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: False)
    monkeypatch.setattr(auditor, "ejecutar_auditor", lambda *a, **k: [])
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", lambda *a, **k: True)

    estado = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert estado["fallo_claude"] is True and estado["fallo_gemini"] is True
    assert estado["resultado_claude"] is None and estado["resultado_gemini"] is None


def test_el_grafo_solo_cuenta_las_auditorias_que_se_guardaron(monkeypatch):
    monkeypatch.setattr(detective, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(detective, "ejecutar_detective", lambda conn, t, m: _resultado_detective() | {
        "contexto": {"empresa_id": 7, "ticker": "FAKE", "nombre": "Fake Inc"}})
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: True)
    monkeypatch.setattr(auditor, "ejecutar_auditor", lambda *a, **k: [{"id": 1}, {"id": 2}])
    guardadas = iter([True, False])
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", lambda *a, **k: next(guardadas))

    estado = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert estado["auditorias_guardadas"] == [{"id": 1}]
