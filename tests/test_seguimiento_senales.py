"""
tests/test_seguimiento_senales.py — la cartera de señales con series de
precios inventadas (sin red ni base de datos): precio en la señal, evolución,
recorrido, una señal por empresa y la lectura avanzada frente al IWM.
"""

from datetime import date, timedelta

import pytest

import seguimiento_senales as ss


def _serie(inicio: date, cierres: list):
    """[(fecha, cierre)] en días consecutivos (para los tests da igual el fin de semana)."""
    return [(inicio + timedelta(days=i), c) for i, c in enumerate(cierres)]


D0 = date(2026, 7, 1)


# ---------- una señal ----------

def test_el_precio_de_la_senal_es_el_ultimo_cierre_del_dia_o_anterior():
    precios = [(D0, 10.0), (D0 + timedelta(days=3), 12.0), (D0 + timedelta(days=4), 15.0)]

    # la señal cae en un día sin cotización (D0+1): vale el último cierre conocido
    m = ss.seguimiento_de_una_senal(D0 + timedelta(days=1), precios)
    assert m["precio_senal"] == 10.0 and m["fecha_referencia"] == D0

    # en un día con cotización, vale el cierre de ese día
    m = ss.seguimiento_de_una_senal(D0 + timedelta(days=3), precios)
    assert m["precio_senal"] == 12.0


def test_el_precio_de_hoy_el_retorno_y_el_multiplo():
    precios = _serie(D0, [10, 11, 12, 20])

    m = ss.seguimiento_de_una_senal(D0, precios)

    assert m["precio_actual"] == 20 and m["retorno"] == pytest.approx(1.0)
    assert m["multiplo"] == pytest.approx(2.0), "una empresa que se duplica es x2"
    assert m["dias_calendario"] == 3


def test_el_recorrido_guarda_la_mayor_subida_y_la_mayor_caida_desde_la_senal():
    precios = _serie(D0, [10, 14, 7, 9])

    m = ss.seguimiento_de_una_senal(D0, precios)

    assert m["max_subida"] == pytest.approx(0.4)
    assert m["max_caida"] == pytest.approx(-0.3)
    assert m["retorno"] == pytest.approx(-0.1), "el retorno actual no es el máximo ni el mínimo"


def test_la_serie_tiene_base_100_en_el_dia_de_la_senal():
    m = ss.seguimiento_de_una_senal(D0, _serie(D0, [10, 12, 15]))

    assert m["serie"] == [(0, 100.0), (1, pytest.approx(120.0)), (2, pytest.approx(150.0))]


def test_el_progreso_hacia_el_anio_llega_como_mucho_al_100_por_100():
    precios = [(D0, 10.0), (D0 + timedelta(days=73), 11.0)]
    assert ss.seguimiento_de_una_senal(D0, precios)["progreso_anio"] == pytest.approx(0.2)

    precios = [(D0, 10.0), (D0 + timedelta(days=500), 11.0)]
    assert ss.seguimiento_de_una_senal(D0, precios)["progreso_anio"] == 1.0


def test_una_senal_de_hoy_se_mide_con_cero_dias_en_vez_de_descartarse():
    """La señal es posterior al último precio: su referencia es ese último cierre
    y el recorrido es 0, pero la empresa ya aparece en la cartera."""
    precios = _serie(D0, [10, 11])

    m = ss.seguimiento_de_una_senal(D0 + timedelta(days=10), precios)

    assert m["precio_senal"] == 11 and m["dias_calendario"] == 0 and m["retorno"] == 0


def test_sin_precio_en_el_dia_de_la_senal_o_antes_no_se_mide():
    assert ss.seguimiento_de_una_senal(D0 - timedelta(days=1), _serie(D0, [10, 11])) is None
    assert ss.seguimiento_de_una_senal(D0, []) is None


# ---------- frente al IWM (lectura avanzada) ----------

def test_el_exceso_es_el_retorno_menos_el_del_iwm():
    precios = _serie(D0, [10, 12, 15])
    bench = _serie(D0, [100, 105, 110])

    m = ss.seguimiento_de_una_senal(D0, precios, bench)

    assert m["retorno"] == pytest.approx(0.5) and m["retorno_bench"] == pytest.approx(0.10)
    assert m["exceso"] == pytest.approx(0.40)


def test_sin_benchmark_todo_lo_demas_sigue_funcionando():
    m = ss.seguimiento_de_una_senal(D0, _serie(D0, [10, 12]), None)

    assert m["retorno"] == pytest.approx(0.2) and m["exceso"] is None


def test_si_al_benchmark_le_falta_un_dia_usa_el_ultimo_conocido():
    precios = _serie(D0, [10, 11, 12])
    bench = [(D0, 100.0), (D0 + timedelta(days=2), 110.0)]  # falta el día 1

    assert ss.seguimiento_de_una_senal(D0, precios, bench)["retorno_bench"] == pytest.approx(0.10)


# ---------- la cartera: una señal por empresa ----------

def _senal(i, ticker, modelo, veredicto, fecha=D0):
    return {"empresa_id": i, "ticker": ticker, "modelo": modelo, "veredicto": veredicto, "fecha": fecha}


def test_la_cartera_cuenta_una_senal_por_empresa_la_primera_vez_que_salio_muy_interesante():
    tarde = D0 + timedelta(days=60)
    senales = [
        _senal(1, "AAA", "claude", "INTERESANTE", D0),                 # no es MUY: no cuenta
        _senal(1, "AAA", "claude", "MUY_INTERESANTE", D0 + timedelta(days=10)),
        _senal(1, "AAA", "gemini", "MUY_INTERESANTE", tarde),          # el re-análisis no duplica
        _senal(2, "BBB", "gemini", "MUY_INTERESANTE", D0),
        _senal(3, "CCC", "claude", "INTERESANTE", D0),                 # nunca fue MUY
    ]

    cartera = ss.primeras_senales(senales)

    assert [c["ticker"] for c in cartera] == ["BBB", "AAA"], "ordenadas por fecha de la primera señal"
    aaa = next(c for c in cartera if c["ticker"] == "AAA")
    assert aaa["fecha"] == D0 + timedelta(days=10), "la fecha es la de la PRIMERA vez"
    assert aaa["modelos"] == ["claude", "gemini"] and aaa["ambos"]
    bbb = next(c for c in cartera if c["ticker"] == "BBB")
    assert bbb["modelos"] == ["gemini"] and not bbb["ambos"]


def test_seguimiento_de_todas_separa_medidas_sin_precios_y_sin_referencia():
    precios = {
        "IWM": _serie(D0, [100] * 6),
        "AAA": _serie(D0, [10, 10, 11, 12, 13, 14]),
        "ANTIGUA": _serie(D0 + timedelta(days=20), [10, 11]),  # sus precios empiezan después de la señal
    }
    senales = [
        _senal(1, "AAA", "claude", "MUY_INTERESANTE", D0 + timedelta(days=2)),
        _senal(2, "ANTIGUA", "claude", "MUY_INTERESANTE", D0),
        _senal(3, "FANTASMA", "claude", "MUY_INTERESANTE", D0),  # Yahoo no tiene nada
    ]

    medidas, sin_precios, sin_referencia = ss.seguimiento_de_todas(senales, precios)

    assert [m["ticker"] for m in medidas] == ["AAA"]
    assert [s["ticker"] for s in sin_precios] == ["FANTASMA"]
    assert [s["ticker"] for s in sin_referencia] == ["ANTIGUA"]


# ---------- consenso y estadísticas (lectura avanzada) ----------

def test_consenso_agrupa_por_cuantos_modelos_dieron_muy_interesante():
    senales = [
        _senal(1, "AAA", "claude", "MUY_INTERESANTE"), _senal(1, "AAA", "gemini", "MUY_INTERESANTE"),
        _senal(2, "BBB", "claude", "MUY_INTERESANTE"), _senal(2, "BBB", "gemini", "INTERESANTE"),
        _senal(3, "CCC", "claude", "INTERESANTE"), _senal(3, "CCC", "gemini", "NADA_INTERESANTE"),
        _senal(4, "DDD", "claude", "MUY_INTERESANTE"),  # solo un modelo: no hay consenso
    ]

    grupos = {s["ticker"]: s["veredicto"] for s in ss.senales_de_consenso(senales)}

    assert grupos == {"AAA": "ambos MUY_INTERESANTE", "BBB": "uno solo MUY_INTERESANTE",
                      "CCC": "ninguno MUY_INTERESANTE"}


def test_consenso_usa_el_ultimo_analisis_de_cada_modelo_y_la_fecha_del_mas_reciente():
    nuevo = D0 + timedelta(days=60)
    senales = [
        _senal(1, "AAA", "claude", "INTERESANTE", D0), _senal(1, "AAA", "claude", "MUY_INTERESANTE", nuevo),
        _senal(1, "AAA", "gemini", "MUY_INTERESANTE", D0 + timedelta(days=3)),
    ]

    (c,) = ss.senales_de_consenso(senales)

    assert c["veredicto"] == "ambos MUY_INTERESANTE" and c["fecha"] == nuevo


def _medida(v, exceso, dias_mercado=30, e1m=None):
    return {"veredicto": v, "exceso": exceso, "dias_mercado": dias_mercado, "dias_calendario": dias_mercado,
            "max_caida": -0.1, "exceso_1s": None, "exceso_1m": e1m, "exceso_3m": None,
            "curva": [(d, exceso) for d in range(dias_mercado + 1)]}


def test_resumen_por_grupo_calcula_la_media_la_mediana_y_el_porcentaje_que_bate():
    filas = ss.resumen_por_grupo([
        _medida("MUY_INTERESANTE", 0.10, e1m=0.05), _medida("MUY_INTERESANTE", -0.20),
        _medida("MUY_INTERESANTE", 0.04, e1m=0.01), _medida("INTERESANTE", -0.02),
    ])

    muy = next(f for f in filas if f["veredicto"] == "MUY_INTERESANTE")
    assert muy["n"] == 3 and muy["exceso_medio"] == pytest.approx(-0.02)
    assert muy["exceso_mediano"] == pytest.approx(0.04) and muy["pct_bate_iwm"] == pytest.approx(2 / 3)
    assert muy["n_1m"] == 2 and muy["exceso_1m"] == pytest.approx(0.03)
    assert muy["exceso_3m"] is None and muy["n_3m"] == 0


def test_las_estadisticas_ignoran_las_senales_con_muy_pocos_dias():
    """Una señal de ayer tiene exceso 0 por construcción: no debe contar como
    'empata con el mercado' en la mediana ni en el % que lo bate."""
    filas = ss.resumen_por_grupo([_medida("X", 0.10), _medida("X", 0.0, dias_mercado=1), _medida("X", None)])

    (x,) = filas
    assert x["n"] == 1 and x["exceso_medio"] == pytest.approx(0.10)


def test_la_curva_media_no_pinta_dias_con_muy_pocas_senales():
    def con_curva(curva):
        return {"veredicto": "X", "exceso": 0.0, "dias_mercado": 10, "curva": curva}

    medidas = [con_curva([(0, 0.0), (1, 0.1), (2, 0.2)]) for _ in range(4)] + [con_curva([(0, 0.0), (1, 0.3)])]

    puntos = ss.curva_media_por_grupo(medidas, min_senales=5)

    assert [(d, n) for _, d, _, n in puntos] == [(0, 5), (1, 5)], "el día 2 solo lo alcanzaron 4 señales"


# ---------- el interruptor: la vista es personal y viene apagada ----------

@pytest.mark.parametrize("valor, esperado", [
    (None, False), ("", False), ("0", False), ("no", False), ("1", True), ("true", True),
    ("TRUE", True), ("sí", True), (" 1 ", True),
])
def test_el_seguimiento_viene_apagado_y_solo_se_activa_a_proposito(monkeypatch, valor, esperado):
    if valor is None:
        monkeypatch.delenv("MOSTRAR_SEGUIMIENTO", raising=False)
    else:
        monkeypatch.setenv("MOSTRAR_SEGUIMIENTO", valor)

    assert ss.activado() is esperado


def test_apagado_no_descarga_nada_de_yahoo(monkeypatch):
    monkeypatch.delenv("MOSTRAR_SEGUIMIENTO", raising=False)

    def no_debe_llamarse(*a, **k):
        raise AssertionError("con el seguimiento apagado no se debe tocar ni la base ni Yahoo")

    monkeypatch.setattr(ss, "actualizar_precios", no_debe_llamarse)
    monkeypatch.setattr(ss, "leer_senales", no_debe_llamarse)

    assert ss.fase_seguimiento() == {"actualizados": 0, "sin_datos": [], "desactivado": True}
