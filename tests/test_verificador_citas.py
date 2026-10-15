"""
tests/test_verificador_citas.py — el verificador de citas v2: cada cita cae
en una categoría, y la etiqueta ALUCINACION depende del % de citas CON
SOPORTE (no del % de citas literales, que es el estricto de la v1).
Sin red ni base de datos.
"""

from datetime import date
from decimal import Decimal

from verificador_citas import verificar_citas, clasificar_cita, _Fuente

FUENTE = (
    "The Company expects revenue growth to accelerate in the second half of fiscal 2026 "
    "as new stores mature and ramp up across all regions. "
    "Gross margin improved year-over-year due to lower freight costs and a favorable product mix. "
    "Revenue 1,200,000 1,050,000 Net loss (340,000) (410,000) Cash 5,800,000 4,100,000."
)
TRANSACCIONES = [("Jane Doe", "CEO", date(2026, 7, 7), Decimal("125000.00"))]


def _tipo(cita, transacciones=None):
    return clasificar_cita(cita, _Fuente(FUENTE), transacciones)


def test_exacta_ignora_mayusculas_y_corchetes_finales():
    assert _tipo("revenue growth to accelerate in the second half of fiscal 2026") == "exacta"
    assert _tipo("Gross margin improved [MD&A, Q2 2026]") == "exacta"


def test_exacta_sin_puntuacion_acepta_otro_guion():
    # La fuente dice year-over-year; el modelo escribió el guion largo
    assert _tipo("Gross margin improved year–over–year due to lower freight costs") \
        == "exacta_sin_puntuacion"


def test_fragmentos_unidos_con_puntos_suspensivos():
    cita = "revenue growth to accelerate in the second half ... lower freight costs and a favorable product mix"
    assert _tipo(cita) == "fragmentos"


def test_fragmentos_con_uno_inexistente_no_cuenta():
    cita = "revenue growth to accelerate in the second half ... a record year for the whole industry worldwide"
    assert _tipo(cita) != "fragmentos"


def test_insiders_coincide_con_el_form_4():
    cita = "Jane Doe (CEO): $125,000 el 2026-07-07"
    assert _tipo(cita, TRANSACCIONES) == "insiders"


def test_insiders_con_importe_inventado_no_cuenta():
    cita = "Jane Doe (CEO): $999,000 el 2026-07-07"
    assert _tipo(cita, TRANSACCIONES) != "insiders"
    assert _tipo("Jane Doe (CEO): $125,000 el 2026-07-07") != "insiders", \
        "sin transacciones que comparar no se puede dar por buena"


def test_editada_casi_literal():
    # Una palabra cambiada en una cita larga: el resto sigue cubierto
    cita = ("The Company expects revenue growth to accelerate in the second half of fiscal 2026 "
            "as new stores mature and ramp up across all areas")
    assert _tipo(cita) == "editada"


def test_tabla_recompuesta_con_cifras_que_estan_juntas():
    assert _tipo("Revenue was $1,200,000 versus $1,050,000 in the prior period") == "tabla"


def test_cifras_sueltas_de_sitios_distintos_no_son_tabla():
    fuente = _Fuente("Revenue 1,200,000 in Q1. " + "filler word " * 100 + "Cash 5,800,000 at year end.")
    assert clasificar_cita("Revenue 1,200,000 and cash 5,800,000 at the end", fuente) != "tabla"


def test_traducida_no_tiene_soporte():
    assert _tipo("Los ingresos aumentaron un 45% por la apertura de nuevas tiendas") == "traducida"


def test_score_del_sistema_no_es_del_documento():
    assert _tipo("Convicción de insiders: 8/10") == "score_del_sistema"


def test_inventada_y_demasiado_corta():
    assert _tipo("the CEO announced a merger with a large technology company") == "sin_soporte"
    assert _tipo("revenue") == "demasiado_corta"


def test_cita_es_no_se_verifica_ni_afecta():
    v = verificar_citas(
        [{"afirmacion": "x", "cita_literal": "Gross margin improved year-over-year",
          "cita_es": "Algo que no esta en el documento para nada"}],
        FUENTE,
    )
    assert v["detalle"][0]["tipo"] == "exacta"
    assert v["detalle"][0]["cita_es"].startswith("Algo")


def test_los_prompts_piden_cita_en_idioma_original_y_traduccion_aparte():
    """Protocolo 2: el 'responde en español' del prompt hacía que los modelos
    tradujeran las citas, y una cita traducida no se puede comprobar. Este
    test congela la corrección para que no se pierda en un refactor."""
    from detective import construir_prompt
    from auditor import construir_prompt_auditor

    contexto = {
        "nombre": "Empresa Ficticia", "ticker": "FAKE", "sector": "x", "bolsa": "NASDAQ",
        "market_cap": 1_000_000, "scores": (1, 2, 3, 4, 10), "transacciones": [],
        "texto_mda": "Revenue grew.", "eventos_8k": [], "activistas": [], "senales_mercado": None,
    }
    prompt_detective = construir_prompt(contexto)
    prompt_auditor = construir_prompt_auditor(contexto, {"veredicto_preliminar": "INTERESANTE"})

    for prompt in (prompt_detective, prompt_auditor):
        assert '"cita_es"' in prompt, "la traducción va en su propio campo"
        assert "NO lo traduzcas" in prompt, "la cita literal va en el idioma del documento"
        assert 'salvo el campo "cita_literal"' in prompt, \
            "el 'en español' del JSON ya no cubre la cita literal"
    assert "No cites los SCORES" in prompt_detective


def _citas(*literales):
    return [{"afirmacion": "a", "cita_literal": c} for c in literales]


def test_etiqueta_alucinacion_por_citas_con_soporte():
    buena = "Gross margin improved year-over-year due to lower freight costs"
    guion = "Gross margin improved year–over–year due to lower freight costs"
    mala = "the CEO announced a merger with a large technology company"

    # 2 de 5 con soporte (40%) -> alucinacion
    v = verificar_citas(_citas(buena, guion, mala, mala + " again", mala + " today"), FUENTE)
    assert v["citas_soportadas"] == 2 and v["alucinacion_detectada"]

    # 3 de 5 (60%) -> no es alucinacion, pero son citas debiles
    v = verificar_citas(_citas(buena, guion, buena, mala, mala + " again"), FUENTE)
    assert v["citas_soportadas"] == 3
    assert not v["alucinacion_detectada"] and v["citas_debiles"]

    # 5 de 5 -> limpio
    v = verificar_citas(_citas(buena, guion, buena, guion, buena), FUENTE)
    assert not v["alucinacion_detectada"] and not v["citas_debiles"]


def test_porcentaje_estricto_sigue_midiendo_solo_las_literales():
    """El estricto es el mismo numero que medía la v1: si solo cuentan las
    exactas, la cita con otro guion NO suma, aunque tenga soporte."""
    buena = "Gross margin improved year-over-year due to lower freight costs"
    guion = "Gross margin improved year–over–year due to lower freight costs"
    v = verificar_citas(_citas(buena, guion), FUENTE)
    assert v["porcentaje_verificado"] == 50.0
    assert v["porcentaje_soportado"] == 100.0
    assert v["version_verificador"] == 2


def test_sin_citas_no_hay_alucinacion():
    v = verificar_citas([], FUENTE)
    assert v["citas_totales"] == 0
    assert not v["alucinacion_detectada"] and not v["citas_debiles"]
