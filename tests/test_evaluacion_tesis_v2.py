"""
tests/test_evaluacion_tesis_v2.py — la evaluación privada de la tesis V2: primeras
marcas por brazo, medición a un horizonte, resumen y bootstrap. Sin base de datos ni red.
"""

from datetime import date, timedelta

import pytest

import orquestador
from evaluacion_tesis_v2 import (
    bootstrap_diferencia, lectura, medidas_de_un_brazo, medir, primeras_marcas, resumen,
)


def _serie(inicio: date, cierres: list) -> list:
    """Serie diaria [(fecha, cierre)] empezando en `inicio`."""
    return [(inicio + timedelta(days=i), c) for i, c in enumerate(cierres)]


def _fila(dia, ticker, v1=False, v2=False, v0=False):
    return {"fecha": dia, "ticker": ticker, "candidata_v1": v1, "candidata_v2": v2, "candidata_v0": v0}


def test_la_empresa_cuenta_desde_la_primera_instantanea_en_que_un_brazo_la_marca():
    filas = [
        _fila(date(2026, 11, 20), "AAA", v1=True),
        _fila(date(2026, 11, 3), "AAA", v1=True, v2=True),   # fuera de orden a propósito
        _fila(date(2026, 12, 1), "BBB", v2=True),
    ]
    m = primeras_marcas(filas)
    assert m["v1"] == {"AAA": date(2026, 11, 3)}
    assert m["v2"] == {"AAA": date(2026, 11, 3), "BBB": date(2026, 12, 1)}
    assert m["v0"] == {}


def test_medir_calcula_retorno_y_exceso_sobre_el_benchmark():
    f0 = date(2026, 11, 3)
    precios = _serie(f0, [10.0] + [10.0] * 399 + [20.0])    # +100 % a los 400 días
    bench = _serie(f0, [100.0] + [100.0] * 399 + [110.0])   # +10 %
    m = medir(f0, precios, bench)
    assert m["retorno"] == pytest.approx(1.0)
    assert m["exceso"] == pytest.approx(0.9)


def test_con_horizonte_no_relleno_si_todavia_no_ha_pasado():
    f0 = date(2026, 11, 3)
    precios = _serie(f0, [10.0] * 100)
    bench = _serie(f0, [100.0] * 100)
    assert medir(f0, precios, bench, dias=365) is None
    assert medir(f0, precios, bench) is not None   # la provisional sí


def test_con_horizonte_mide_en_la_fecha_del_horizonte_y_no_al_final():
    f0 = date(2026, 11, 3)
    cierres = [10.0] * 366 + [50.0] * 100     # sube mucho DESPUÉS de los 365 días
    precios = _serie(f0, cierres)
    bench = _serie(f0, [100.0] * len(cierres))
    m = medir(f0, precios, bench, dias=365)
    assert m["retorno"] == pytest.approx(0.0)


def test_precio_de_partida_es_el_ultimo_cierre_en_el_dia_o_antes():
    f_marca = date(2026, 11, 7)   # sábado: sin cierre ese día
    precios = [(date(2026, 11, 6), 10.0), (date(2026, 11, 9), 12.0)]
    bench = [(date(2026, 11, 6), 100.0), (date(2026, 11, 9), 100.0)]
    m = medir(f_marca, precios, bench)
    assert m["retorno"] == pytest.approx(0.2)   # parte del cierre del 6, no del 9


def test_sin_precios_o_sin_precio_de_partida_devuelve_none():
    f0 = date(2026, 11, 3)
    bench = _serie(f0, [100.0] * 5)
    assert medir(f0, [], bench) is None
    assert medir(f0, _serie(f0 + timedelta(days=10), [10.0] * 5), bench) is None   # empieza tarde


def test_las_empresas_sin_medir_se_cuentan_aparte():
    f0 = date(2026, 11, 3)
    bench = _serie(f0, [100.0] * 10)
    precios = {"OK": _serie(f0, [10.0] * 9 + [11.0])}
    medidas, sin_medir = medidas_de_un_brazo({"OK": f0, "FALTA": f0}, precios, bench)
    assert set(medidas) == {"OK"} and sin_medir == ["FALTA"]


def test_resumen_cuenta_las_que_doblan():
    medidas = {"A": {"retorno": 1.5, "exceso": 1.4}, "B": {"retorno": 1.0, "exceso": 0.9},
               "C": {"retorno": -0.2, "exceso": -0.3}, "D": {"retorno": 0.1, "exceso": 0.0}}
    r = resumen(medidas)
    assert r["n"] == 4 and r["pct_dobla"] == 0.5
    assert r["mediana_retorno"] == pytest.approx(0.55)
    assert resumen({})["n"] == 0 and resumen({})["mediana_exceso"] is None


def _medidas(excesos: dict) -> dict:
    return {t: {"retorno": e, "exceso": e} for t, e in excesos.items()}


def test_bootstrap_es_reproducible_y_el_intervalo_contiene_al_punto():
    a = _medidas({f"A{i}": 0.5 + i * 0.1 for i in range(10)})
    b = _medidas({f"B{i}": 0.0 + i * 0.1 for i in range(10)})
    r1 = bootstrap_diferencia(a, b, remuestreos=500, semilla=1)
    r2 = bootstrap_diferencia(a, b, remuestreos=500, semilla=1)
    assert r1 == r2
    punto, bajo, alto, n = r1
    assert bajo <= punto <= alto and n > 0
    assert bajo > 0 and "a favor del primero" in lectura(r1)   # diferencia grande y clara


def test_bootstrap_con_brazos_iguales_incluye_el_cero():
    a = _medidas({f"X{i}": i * 0.1 for i in range(12)})
    r = bootstrap_diferencia(a, dict(a), remuestreos=500, semilla=1)
    assert r[0] == 0 and "no concluyente" in lectura(r)


def test_bootstrap_sin_datos_en_algun_brazo():
    assert bootstrap_diferencia({}, _medidas({"A": 1.0})) is None
    assert lectura(None) == "sin datos"


def test_la_fase_de_la_tesis_v2_nunca_rompe_la_pasada(monkeypatch):
    def sin_base_de_datos():
        raise RuntimeError("sin conexión")

    monkeypatch.setattr(orquestador, "conectar_db", sin_base_de_datos)
    assert orquestador.fase_tesis_v2() == 0
