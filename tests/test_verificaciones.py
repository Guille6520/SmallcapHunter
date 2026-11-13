"""
tests/test_verificaciones.py — el Auditor verificador: que el código marque las cifras que
no están en el texto, que el modelo solo pueda absolverlas con un cálculo comprobable,
y que el sello (VERIFICADO / CON_AVISOS) salga de eso y no de la opinión del modelo.

Sin base de datos ni modelos: el texto fuente y las respuestas del "modelo" son
diccionarios, y el verificador de citas es el de verdad.
"""

import json

import pytest

import verificaciones
from verificacion_cifras import (
    aparece, bloque_estructurado, cifras_sin_soporte, derivacion_verificable, valores_del_texto,
)
from verificador_citas import verificar_citas
from verificaciones import SELLO_CON_AVISOS, SELLO_VERIFICADO, citas_del_auditor, valorar

FUENTE = (
    "Revenue increased 12% compared to the prior year driven by new enterprise customers. "
    "Total revenue was $48.2 million and prior year revenue was $43.0 million. "
    "Management disclosed substantial doubt about the company's ability to continue as a going concern."
)
CITA_REAL = "Revenue increased 12% compared to the prior year driven by new enterprise customers"
CITA_FALSA = "The chief executive officer announced a merger with a large technology company"


def _valorar(resp, sospechosas=()):
    sospechosas = list(sospechosas)
    ver = verificar_citas(citas_del_auditor(resp), FUENTE)
    return valorar(resp, FUENTE, sospechosas, ver)


# ---------- la parte de código: ¿está la cifra en el texto? ----------

def test_una_cifra_que_esta_en_el_texto_no_se_marca_aunque_cambie_la_escala():
    assert aparece(48.2e6, valores_del_texto(FUENTE)), "«$48.2 million» en la tesis, «48.2» en el texto"
    assert aparece(12, valores_del_texto(FUENTE))
    assert not aparece(34, valores_del_texto(FUENTE))


def test_una_cifra_inventada_se_marca_y_una_real_no():
    tesis = "Los ingresos crecieron un 12% hasta $48.2 million y el margen subió un 34%"

    marcadas = cifras_sin_soporte(tesis, FUENTE)

    assert [m["cifra"] for m in marcadas] == ["34%"]
    assert "margen" in marcadas[0]["contexto"]


def test_los_anos_y_los_valores_pequenos_no_son_cifras_que_comprobar():
    assert cifras_sin_soporte("Desde 2024 la empresa gana un 3% más", FUENTE) == []


def test_los_sufijos_k_m_b_cuentan_como_miles_millones_y_miles_de_millones():
    # «$3.2M» antes se leía como 3,2, caía bajo el umbral de valores pequeños y no se
    # comprobaba nunca; con «million» escrito sí se comprobaba.
    assert [m["cifra"] for m in cifras_sin_soporte("El CEO compró $3.2M en acciones", FUENTE)] == ["$3.2M"]
    assert [m["cifra"] for m in cifras_sin_soporte("El CEO compró $4M en acciones", FUENTE)] == ["$4M"]
    assert [m["cifra"] for m in cifras_sin_soporte("Una deuda de $2B", FUENTE)] == ["$2B"]
    # La cifra marcada conserva su sufijo (antes se mostraba «$280» en vez de «$280k»).
    assert [m["cifra"] for m in cifras_sin_soporte("Compras por $280k", FUENTE)] == ["$280k"]


def test_una_cifra_con_sufijo_que_esta_en_el_texto_no_se_marca():
    assert cifras_sin_soporte("Ingresos de $48.2M", FUENTE) == []
    assert cifras_sin_soporte("Ingresos de $48.2 million", FUENTE) == []
    assert cifras_sin_soporte("Ingresos de $48,200k", "Revenue was 48,200 in thousands") == []


def test_una_letra_suelta_tras_un_importe_no_es_un_sufijo():
    # «$100 bought» no es «$100 billion» ni «$100B».
    marcadas = cifras_sin_soporte("Por $100 bought shares", FUENTE)
    assert [m["cifra"] for m in marcadas] == ["$100"]


def test_el_resultado_de_un_calculo_con_sufijo_no_cuenta_como_operando():
    fv = valores_del_texto("Cash 2,500 and 5,500 and 3,500. Purchases of 280,000 shares")
    # 280000 es el propio resultado de «$280k»; sin contarlo quedan solo 2 operandos reales
    assert derivacion_verificable("2,500 + 5,500 = 8,000 y 280,000", "$280k", fv)
    assert not derivacion_verificable("280,000 y 2,500", "$280k", fv), "con un solo operando real no basta"


def test_el_modelo_solo_absuelve_una_cifra_si_su_calculo_se_apoya_en_el_texto():
    fv = valores_del_texto(FUENTE)
    assert derivacion_verificable("48.2 / 43.0 - 1 = 12.1%", "12.1%", fv)
    assert not derivacion_verificable("es lo normal en el sector", "12.1%", fv), "sin cálculo no hay absolución"
    assert not derivacion_verificable("77 / 31 - 1 = 12.1%", "12.1%", fv), "operandos que no están en el texto"
    assert not derivacion_verificable("48.2 - 1 = 12.1%", "12.1%", fv), "con un solo operando real no basta"


def test_el_bloque_estructurado_tolera_datos_que_faltan():
    texto = bloque_estructurado({"sector": "Tech", "bolsa": "NASDAQ", "market_cap": None,
                                 "scores": None, "transacciones": [("Ana", None, "2026-01-02", None),
                                                                   ("Luis", "CFO", "2026-01-03", 512000)]})

    assert "no disponible" in texto and "TOTAL 0/40" in texto
    assert "Luis (CFO): $512,000" in texto and "Ana" not in texto, "sin importe no se lista"


# ---------- qué citas se verifican ----------

def test_se_verifican_las_citas_de_afirmaciones_cifras_contradichas_y_riesgos_pero_no_las_vacias():
    resp = {
        "afirmaciones": [
            {"afirmacion": "a", "estado": "RESPALDADA", "cita_literal": "cita a"},
            {"afirmacion": "b", "estado": "SIN_RESPALDO", "cita_literal": "esta se ignora"},
            {"afirmacion": "c", "estado": "CONTRADICHA", "cita_literal": "cita c"},
            {"afirmacion": "d", "estado": "RESPALDADA", "cita_literal": ""},
        ],
        "cifras": [{"cifra": "9%", "estado": "CONTRADICHA", "cita_literal": "cita cifra"},
                   {"cifra": "8%", "estado": "SIN_SOPORTE", "cita_literal": "no cuenta"}],
        "riesgos_omitidos": [{"riesgo": "r", "cita_literal": "cita riesgo"}, {"riesgo": "sin cita"}],
    }

    assert [c["cita_literal"] for c in citas_del_auditor(resp)] == ["cita a", "cita c", "cita cifra", "cita riesgo"]


# ---------- el sello ----------

def test_una_auditoria_sin_nada_que_objetar_es_verificada():
    resp = {"afirmaciones": [{"afirmacion": "los ingresos subieron", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "cifras": [], "riesgos_omitidos": []}

    v = _valorar(resp)

    assert v["sello"] == SELLO_VERIFICADO and v["n_avisos"] == 0
    assert v["respaldadas"] == 1 and v["respaldadas_con_cita_falsa"] == 0


def test_una_cifra_derivada_con_calculo_comprobable_queda_absuelta():
    marcadas = [{"cifra": "12.1%", "contexto": "crecimiento del 12.1%"}]
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "cifras": [{"cifra": "12.1%", "estado": "DERIVADA_CORRECTA", "explicacion": "48.2 / 43.0 - 1 = 12.1%"}]}

    v = _valorar(resp, marcadas)

    assert v["sello"] == SELLO_VERIFICADO and v["cifras_absueltas"] == 1


def test_una_cifra_absuelta_con_un_calculo_inventado_sigue_marcada():
    """Es el truco que el diseño impide: «es derivada» sin enseñar números que estén en el texto."""
    marcadas = [{"cifra": "34%", "contexto": "el margen subió un 34%"}]
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "cifras": [{"cifra": "34%", "estado": "DERIVADA_CORRECTA", "explicacion": "77 / 57 = 34%"}]}

    v = _valorar(resp, marcadas)

    assert v["sello"] == SELLO_CON_AVISOS
    assert v["avisos_cifras"][0]["motivo"] == "derivacion_sin_base"


@pytest.mark.parametrize("estado, motivo", [("SIN_SOPORTE", "sin_soporte"), ("CONTRADICHA", "contradicha")])
def test_una_cifra_que_el_modelo_confirma_como_error_da_aviso(estado, motivo):
    marcadas = [{"cifra": "34%", "contexto": "el margen subió un 34%"}]
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "cifras": [{"cifra": "34%", "estado": estado, "explicacion": "no está", "cita_literal": CITA_REAL}]}

    v = _valorar(resp, marcadas)

    assert v["sello"] == SELLO_CON_AVISOS and v["avisos_cifras"][0]["motivo"] == motivo


def test_una_cifra_marcada_que_el_modelo_ni_menciona_no_se_da_por_buena():
    marcadas = [{"cifra": "34%", "contexto": "el margen subió un 34%"}]
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}], "cifras": []}

    v = _valorar(resp, marcadas)

    assert v["sello"] == SELLO_CON_AVISOS and v["avisos_cifras"][0]["motivo"] == "sin_adjudicar"
    assert "margen" in v["avisos_cifras"][0]["contexto"]


def test_las_cifras_que_el_modelo_comenta_por_su_cuenta_no_cuentan():
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "cifras": [{"cifra": "99%", "estado": "SIN_SOPORTE", "explicacion": "me la he inventado yo"}]}

    assert _valorar(resp, [])["sello"] == SELLO_VERIFICADO


def test_una_afirmacion_contradicha_con_cita_real_da_aviso():
    resp = {"afirmaciones": [{"afirmacion": "la empresa no tiene dudas de liquidez", "estado": "CONTRADICHA",
                              "cita_literal": "Management disclosed substantial doubt about the company's ability to continue as a going concern"}]}

    v = _valorar(resp)

    assert v["sello"] == SELLO_CON_AVISOS
    assert v["afirmaciones_contradichas"][0]["afirmacion"].startswith("la empresa no tiene")


def test_una_afirmacion_contradicha_con_una_cita_que_no_existe_no_cuenta():
    """Si el modelo dice «el texto lo contradice» pero la cita es inventada, no es un hallazgo."""
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "CONTRADICHA", "cita_literal": CITA_FALSA}]}

    assert _valorar(resp)["sello"] == SELLO_VERIFICADO


def test_una_afirmacion_respaldada_con_cita_falsa_se_cuenta_aparte():
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_FALSA}]}

    assert _valorar(resp)["respaldadas_con_cita_falsa"] == 1


def test_un_riesgo_omitido_solo_se_ensena_si_su_cita_existe():
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}],
            "riesgos_omitidos": [
                {"riesgo": "dudas de continuidad", "cita_literal": "Management disclosed substantial doubt about the company's ability to continue as a going concern"},
                {"riesgo": "una fusión que nadie ha anunciado", "cita_literal": CITA_FALSA},
            ]}

    v = _valorar(resp)

    assert [r["riesgo"] for r in v["riesgos_omitidos"]] == ["dudas de continuidad"]
    assert v["sello"] == SELLO_VERIFICADO, "un riesgo omitido informa, no invalida el análisis"


def test_el_resumen_se_puede_guardar_como_json():
    resp = {"afirmaciones": [{"afirmacion": "x", "estado": "RESPALDADA", "cita_literal": CITA_REAL}]}

    json.dumps(_valorar(resp))


# ---------- la tabla ----------

class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        if self.conn.falla:
            raise RuntimeError("la base se cayó")
        self.conn.sql.append((sql, params))

    def fetchone(self):
        return (1,) if self.conn.hay else None

    def close(self):
        pass


class _Conn:
    def __init__(self, falla=False, hay=False):
        self.falla, self.hay, self.sql, self.commits, self.rollbacks = falla, hay, [], 0, 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _resultado():
    return {"empresa_id": 7, "detective_auditoria_id": 11, "modelo_detective": "claude", "modelo_auditor": "gemini",
            "sello": SELLO_CON_AVISOS, "resumen": {"n_avisos": 1}, "respuesta_llm": {"afirmaciones": []},
            "verificacion_citas": {"citas_totales": 0}}


def test_el_esquema_se_crea_una_sola_vez_por_proceso(monkeypatch):
    monkeypatch.setattr(verificaciones, "_esquema_listo", False)
    conn = _Conn()

    verificaciones.asegurar_esquema(conn)
    verificaciones.asegurar_esquema(conn)

    assert len(conn.sql) == 1 and "create table if not exists verificaciones" in conn.sql[0][0]
    assert conn.commits == 1


def test_si_el_esquema_falla_se_deshace_y_se_avisa(monkeypatch):
    monkeypatch.setattr(verificaciones, "_esquema_listo", False)
    conn = _Conn(falla=True)

    with pytest.raises(RuntimeError):
        verificaciones.asegurar_esquema(conn)

    assert conn.rollbacks == 1 and verificaciones._esquema_listo is False


def test_guardar_inserta_el_sello_con_la_version_y_no_duplica(monkeypatch):
    monkeypatch.setattr(verificaciones, "_esquema_listo", True)
    conn = _Conn()

    assert verificaciones.guardar(conn, _resultado()) is True

    sql, params = conn.sql[0]
    assert "insert into verificaciones" in sql and "on conflict" in sql and "do nothing" in sql
    assert params[:6] == (7, 11, "claude", "gemini", verificaciones.VERSION_AUDITOR, SELLO_CON_AVISOS)
    assert conn.commits == 1


def test_ya_verificado_distingue_por_analisis_modelo_y_version(monkeypatch):
    monkeypatch.setattr(verificaciones, "_esquema_listo", True)

    assert verificaciones.ya_verificado(_Conn(hay=True), 11, "gemini") is True
    assert verificaciones.ya_verificado(_Conn(hay=False), 11, "gemini") is False
    conn = _Conn()
    verificaciones.ya_verificado(conn, 11, "gemini")
    assert conn.sql[0][1] == (11, "gemini", verificaciones.VERSION_AUDITOR)
