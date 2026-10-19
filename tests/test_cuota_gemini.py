"""
tests/test_cuota_gemini.py — la cuota gratuita diaria de Gemini: qué cuesta
analizar una empresa, qué cabe en lo que queda hoy y cómo recorta la pasada
el orquestador. La BD y el grafo van simulados.
"""

import cuota_gemini
import orquestador

CONFIG = {"corte": 25, "max_analisis": 10, "pausa_llm": 0, "gemini_limite": 20}


def test_cuesta_gemini_lo_que_dice_el_docstring():
    # Le tocan los dos modelos: Detective de Gemini + auditoría de Gemini sobre Claude
    assert cuota_gemini.llamadas_gemini_de_una_empresa(["claude", "gemini"]) == 2
    # Solo le toca Claude: queda la auditoría de Gemini sobre ese análisis
    assert cuota_gemini.llamadas_gemini_de_una_empresa(["claude"]) == 1
    # Una auditoría pendiente gasta cuota de Gemini si el análisis a auditar es de Claude
    assert cuota_gemini.llamadas_gemini_de_una_auditoria(["claude"]) == 1
    assert cuota_gemini.llamadas_gemini_de_una_auditoria(["gemini"]) == 0
    assert cuota_gemini.llamadas_gemini_de_una_auditoria(["claude", "gemini"]) == 1


def test_recortar_deja_pasar_lo_que_cabe_en_orden():
    pendientes = [("A", 2), ("B", 2), ("C", 2)]
    caben, aplazadas = cuota_gemini.recortar_por_cuota(pendientes, 5, lambda p: p[1])
    assert caben == [("A", 2), ("B", 2)] and aplazadas == [("C", 2)]


def test_una_cara_que_no_cabe_no_frena_a_una_barata_que_si():
    pendientes = [("cara", 2), ("barata", 1)]
    caben, aplazadas = cuota_gemini.recortar_por_cuota(pendientes, 1, lambda p: p[1])
    assert caben == [("barata", 1)] and aplazadas == [("cara", 2)]


def test_sin_cuota_no_entra_nada():
    caben, aplazadas = cuota_gemini.recortar_por_cuota([("A", 1)], 0, lambda p: p[1])
    assert caben == [] and aplazadas == [("A", 1)]


# ---------- el orquestador ----------

class _Conn:
    def close(self):
        pass


def _preparar(monkeypatch, usadas, pendientes):
    intentados = []
    monkeypatch.setattr(orquestador, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(orquestador, "empresas_que_necesitan_analisis", lambda *a, **k: pendientes)
    monkeypatch.setattr(cuota_gemini, "usadas_hoy", lambda conn: usadas)

    def fake_grafo(ticker, modelos):
        intentados.append(ticker)
        exitos = {m: True for m in modelos}
        exitos["auditorias_guardadas"] = 0
        return exitos

    monkeypatch.setattr(orquestador, "ejecutar_grafo_paso", fake_grafo)
    monkeypatch.setattr(orquestador.time, "sleep", lambda s: None)
    return intentados


def _pendientes(n):
    return [(i, f"T{i}", 30 - i, ["claude", "gemini"]) for i in range(n)]


def test_la_pasada_se_recorta_a_la_cuota_que_queda(monkeypatch):
    # 15 usadas de 20 -> quedan 5 -> a 2 llamadas por empresa caben 2
    intentados = _preparar(monkeypatch, 15, _pendientes(6))

    resultado = orquestador.fase_agentes(CONFIG)

    assert intentados == ["T0", "T1"]
    assert resultado == {"analizadas": 2, "fallos": 0, "aplazadas_por_cuota": 4}


def test_cuota_agotada_no_lanza_ningun_analisis(monkeypatch):
    intentados = _preparar(monkeypatch, 20, _pendientes(3))

    resultado = orquestador.fase_agentes(CONFIG)

    assert intentados == []
    assert resultado == {"analizadas": 0, "fallos": 0, "aplazadas_por_cuota": 3}


def test_limite_cero_desactiva_el_recorte(monkeypatch):
    """Cuenta de Gemini con facturación: no hay cuota que proteger."""
    intentados = _preparar(monkeypatch, 500, _pendientes(3))

    resultado = orquestador.fase_agentes({**CONFIG, "gemini_limite": 0})

    assert intentados == ["T0", "T1", "T2"]
    assert resultado == {"analizadas": 3, "fallos": 0}


def test_sin_aplazadas_el_resultado_no_cambia(monkeypatch):
    """Hay cuota de sobra: el resultado es el de siempre (lo comprueban los
    tests de fase_agentes con igualdad exacta)."""
    _preparar(monkeypatch, 0, _pendientes(2))

    assert orquestador.fase_agentes(CONFIG) == {"analizadas": 2, "fallos": 0}


def test_las_auditorias_pendientes_tambien_se_recortan(monkeypatch):
    llamadas = []
    monkeypatch.setattr(orquestador, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(orquestador, "auditorias_pendientes",
                        lambda *a, **k: [("A", ["claude"]), ("B", ["claude"]), ("C", ["gemini"])])
    monkeypatch.setattr(cuota_gemini, "usadas_hoy", lambda conn: 19)
    monkeypatch.setattr(orquestador, "ejecutar_grafo_paso",
                        lambda t, m: llamadas.append(t) or {"auditorias_guardadas": 1})
    monkeypatch.setattr(orquestador.time, "sleep", lambda s: None)

    resultado = orquestador.fase_auditorias(CONFIG)

    # A cabe (1 llamada de Gemini), B ya no, C no gasta cuota de Gemini
    assert llamadas == ["A", "C"] and resultado["auditadas"] == 2
