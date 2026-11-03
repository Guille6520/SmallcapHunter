"""
tests/test_tesis_v2.py — la tesis V2 en paralelo: los tramos del score de precio de
cada brazo y la comparación de conjuntos de candidatas. Sin base de datos.
"""

import pytest

from scorer_v2 import (
    BRAZOS, comparar, score_precio_v0, score_precio_v1, score_precio_v2, totales,
)


@pytest.mark.parametrize("pos, v1, v2", [
    (0.00, 10, 0), (0.10, 10, 0), (0.15, 10, 0), (0.19, 8, 0), (0.20, 8, 2), (0.30, 8, 2),
    (0.40, 6, 4), (0.45, 6, 4), (0.55, 4, 6), (0.60, 4, 6), (0.70, 2, 8), (0.80, 2, 8),
    (0.85, 0, 10), (0.95, 0, 10), (1.00, 0, 10),
])
def test_tramos_de_v1_y_v2(pos, v1, v2):
    assert score_precio_v1(pos) == v1
    assert score_precio_v2(pos) == v2


def test_v2_es_el_espejo_exacto_de_v1():
    """score_precio_v2(p) == score_precio_v1(1 - p) en toda la escala, cortes incluidos.
    Es la forma más simple de la hipótesis contraria, a propósito."""
    for i in range(0, 101):
        p = i / 100
        assert score_precio_v2(p) == score_precio_v1(round(1 - p, 10)), p


def test_sin_dato_v1_y_v2_dan_cero_y_el_control_da_cinco():
    assert score_precio_v1(None) == 0 and score_precio_v2(None) == 0
    assert score_precio_v0(None) == 5 and score_precio_v0(0.9) == 5


def test_v2_no_baja_al_acercarse_a_maximos():
    puntos = [i / 100 for i in range(0, 101)]
    valores = [score_precio_v2(p) for p in puntos]
    assert valores == sorted(valores), "V2 debe ser no decreciente con la posición"
    valores_v1 = [score_precio_v1(p) for p in puntos]
    assert valores_v1 == sorted(valores_v1, reverse=True), "V1 debe ser no creciente"


def _fila(ticker, pos, sc=8, st=8, scat=8):
    return {"ticker": ticker, "posicion_52w": pos, "score_conviccion": sc, "score_temporal": st,
            "score_catalizador": scat}


def test_los_totales_solo_se_diferencian_en_el_score_de_precio():
    t = totales(_fila("A", 0.05))
    assert t == {"v1": 24 + 10, "v2": 24 + 0, "v0": 24 + 5}
    assert set(t) == set(BRAZOS)


def test_comparar_separa_lo_que_marca_cada_brazo():
    filas = [
        _fila("MINIMOS", 0.05),            # V1: 34, V2: 24, V0: 29
        _fila("MAXIMOS", 0.95),            # V1: 24, V2: 34, V0: 29
        _fila("MEDIA", 0.50, sc=9, st=9, scat=9),  # V1: 31, V2: 33, V0: 32
        _fila("FLOJA", 0.50, sc=2, st=2, scat=2),  # V1: 10, V2: 12, V0: 11
    ]

    c = comparar(filas, corte=25)

    assert c["marcadas"]["v1"] == {"MINIMOS", "MEDIA"}
    assert c["marcadas"]["v2"] == {"MAXIMOS", "MEDIA"}
    assert c["marcadas"]["v0"] == {"MINIMOS", "MAXIMOS", "MEDIA"}
    assert c["en_v1_y_v2"] == {"MEDIA"}
    assert c["solo_v1"] == {"MINIMOS"} and c["solo_v2"] == {"MAXIMOS"}


def test_el_corte_es_inclusivo():
    c = comparar([_fila("JUSTO", 0.05, sc=5, st=5, scat=5)], corte=25)  # V1: 15 + 10 = 25
    assert c["marcadas"]["v1"] == {"JUSTO"}
