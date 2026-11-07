"""
tests/test_votos.py — que el veredicto que enseña el dashboard sea el de los
Detectives y no el del Auditor, y que el orden de la lista no dependa del que
escribió último.
"""

import pandas as pd

from votos import ORDEN_VEREDICTO, mejor_voto, texto_votos


def test_el_mejor_voto_es_el_mas_favorable_de_los_dos():
    assert mejor_voto("INTERESANTE", "MUY_INTERESANTE") == "MUY_INTERESANTE"
    assert mejor_voto("NADA_INTERESANTE", "INTERESANTE") == "INTERESANTE"
    assert mejor_voto("NADA_INTERESANTE", "NADA_INTERESANTE") == "NADA_INTERESANTE"


def test_con_un_solo_voto_vale_ese():
    assert mejor_voto("INTERESANTE", None) == "INTERESANTE"
    assert mejor_voto(None, "MUY_INTERESANTE") == "MUY_INTERESANTE"


def test_sin_votos_es_none_y_un_valor_raro_no_cuenta():
    assert mejor_voto(None, None) is None
    assert mejor_voto(float("nan"), "ESTO_NO_EXISTE") is None
    assert mejor_voto("ESTO_NO_EXISTE", "INTERESANTE") == "INTERESANTE"


def test_una_alucinacion_pierde_contra_cualquier_veredicto_real():
    assert mejor_voto("ALUCINACION", "NADA_INTERESANTE") == "NADA_INTERESANTE"


def test_el_orden_de_la_lista_pone_antes_lo_mas_interesante_y_las_sin_analizar_en_medio():
    candidatas = pd.DataFrame({
        "ticker": ["A", "B", "C", "D"],
        "voto_claude": ["NADA_INTERESANTE", None, "MUY_INTERESANTE", "INTERESANTE"],
        "voto_gemini": [None, None, "INTERESANTE", "INTERESANTE"],
        "score_total": [30, 35, 26, 28],
    })
    candidatas["veredicto"] = [mejor_voto(c, g) for c, g in zip(candidatas.voto_claude, candidatas.voto_gemini)]
    candidatas["orden"] = candidatas["veredicto"].fillna("SIN_ANALIZAR").map(ORDEN_VEREDICTO)

    orden = candidatas.sort_values(["orden", "score_total"], ascending=[True, False])["ticker"].tolist()

    assert orden == ["C", "D", "B", "A"]


def test_el_texto_de_los_votos_dice_cuando_falta_uno():
    assert texto_votos("MUY_INTERESANTE", None) == "Claude: MUY_INTERESANTE · Gemini: sin voto"
    # pandas deja un hueco como NaN, no como None
    assert texto_votos(float("nan"), "INTERESANTE") == "Claude: sin voto · Gemini: INTERESANTE"


# ---------- los sellos del Auditor ----------

def test_el_sello_se_pega_a_la_etiqueta_del_voto_y_sin_sello_no_se_toca():
    from votos import con_sello, marca_sello

    assert con_sello("🟡 INTERESANTE", "VERIFICADO") == "🟡 INTERESANTE ✅"
    assert con_sello("🟡 INTERESANTE", "CON_AVISOS") == "🟡 INTERESANTE ⚠️"
    assert con_sello("🟡 INTERESANTE", None) == "🟡 INTERESANTE"
    assert con_sello("⚪ sin voto", float("nan")) == "⚪ sin voto", "pandas deja un hueco como NaN"
    assert marca_sello("ALGO_RARO") == ""


def test_el_texto_del_sello_dice_quien_lo_puso_y_cuantos_avisos_hay():
    from votos import texto_sello

    assert texto_sello("VERIFICADO", 0, "gemini") == "✅ Verificado por gemini"
    assert texto_sello("CON_AVISOS", 2, "claude") == "⚠️ 2 avisos según claude"
    assert texto_sello("CON_AVISOS", 1, "claude") == "⚠️ 1 aviso según claude"
    assert "Sin verificar" in texto_sello(None)
