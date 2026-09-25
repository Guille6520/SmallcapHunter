"""
tests/test_orquestador_fase_agentes.py — cuándo se retira un modelo del
resto de una pasada. Antes se retiraba al primer fallo asumiendo cuota
agotada, y en la pasada real del 24-sep un 503 suelto de Gemini y un JSON
mal formado de Nemotron sacaron a los dos y 6 de 10 empresas quedaron sin
tocar. La BD, el grafo y los sleeps van simulados: lo que pruebo es la
política de retirada, no las llamadas a los LLM.
"""

import orquestador

CONFIG = {"corte": 25, "max_analisis": 10, "pausa_llm": 0}


def _preparar(monkeypatch, resultados):
    """resultados: lista de (ticker, exito_de_groq). Devuelve la lista de
    tickers que llegaron a intentarse."""
    intentados = []

    class _Conn:
        def close(self):
            pass

    pendientes = [
        (i, ticker, 30 - i, ["groq"]) for i, (ticker, _) in enumerate(resultados)
    ]
    exito_por_ticker = dict(resultados)

    def fake_grafo(ticker, modelos):
        intentados.append(ticker)
        exitos = {m: exito_por_ticker[ticker] for m in modelos}
        exitos["auditorias_guardadas"] = 0
        return exitos

    monkeypatch.setattr(orquestador, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(orquestador, "empresas_que_necesitan_analisis", lambda *a, **k: pendientes)
    monkeypatch.setattr(orquestador, "ejecutar_grafo_paso", fake_grafo)
    monkeypatch.setattr(orquestador.time, "sleep", lambda s: None)
    return intentados


def test_un_fallo_suelto_no_retira_al_modelo(monkeypatch):
    intentados = _preparar(monkeypatch, [("A", False), ("B", True), ("C", True)])

    resultado = orquestador.fase_agentes(CONFIG)

    assert intentados == ["A", "B", "C"]
    assert resultado == {"analizadas": 2, "fallos": 1}


def test_fallos_seguidos_retiran_al_modelo(monkeypatch):
    n = orquestador.FALLOS_SEGUIDOS_PARA_RETIRAR
    tickers = [(f"T{i}", False) for i in range(n + 2)]
    intentados = _preparar(monkeypatch, tickers)

    resultado = orquestador.fase_agentes(CONFIG)

    assert intentados == [t for t, _ in tickers[:n]], \
        "tras n fallos seguidos el modelo se retira y no se intenta el resto"
    assert resultado == {"analizadas": 0, "fallos": n}


def test_un_exito_reinicia_la_cuenta_de_fallos(monkeypatch):
    n = orquestador.FALLOS_SEGUIDOS_PARA_RETIRAR
    exitos = [False] * (n - 1) + [True] + [False] * (n - 1)
    tickers = [(f"T{i}", ok) for i, ok in enumerate(exitos)]
    intentados = _preparar(monkeypatch, tickers)

    resultado = orquestador.fase_agentes(CONFIG)

    assert intentados == [t for t, _ in tickers], \
        "sin n fallos seguidos nadie se retira, aunque en total fallen varios"
    assert resultado == {"analizadas": 1, "fallos": 2 * (n - 1)}
