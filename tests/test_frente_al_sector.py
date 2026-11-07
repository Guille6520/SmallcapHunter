"""
tests/test_frente_al_sector.py — la comparación privada de cada empresa de la
cartera con el ETF de su sector: si la acción sube, ¿es la empresa o es todo su
sector? Series inventadas, sin red; la parte de base de datos con un cursor falso.
"""

from datetime import date, timedelta

import seguimiento_senales as ss

D0 = date(2026, 7, 1)


def _serie(cierres):
    return [(D0 + timedelta(days=i), c) for i, c in enumerate(cierres)]


def test_una_empresa_que_sube_lo_mismo_que_su_sector_no_aporta_nada_propio():
    empresa = _serie([10, 12, 15])             # +50 %
    sector = _serie([100, 120, 150])           # +50 %

    f = ss.frente_al_sector(D0, empresa, sector)

    assert round(f["retorno"], 4) == 0.5 and round(f["retorno_sector"], 4) == 0.5
    assert abs(f["exceso_sector"]) < 1e-9
    assert ss.lectura_frente_al_sector(f["exceso_sector"]) == "con su sector"


def test_una_empresa_que_sube_mucho_mas_que_su_sector_va_mejor():
    f = ss.frente_al_sector(D0, _serie([10, 20]), _serie([100, 110]))

    assert round(f["exceso_sector"], 4) == 0.9
    assert ss.lectura_frente_al_sector(f["exceso_sector"]) == "mejor que su sector"


def test_una_empresa_que_sube_menos_que_su_sector_va_peor_aunque_suba():
    f = ss.frente_al_sector(D0, _serie([10, 11]), _serie([100, 130]))

    assert f["retorno"] > 0 and f["exceso_sector"] < 0
    assert ss.lectura_frente_al_sector(f["exceso_sector"]) == "peor que su sector"


def test_tambien_da_el_exceso_sobre_el_iwm_en_el_mismo_periodo():
    f = ss.frente_al_sector(D0, _serie([10, 15]), _serie([100, 110]), _serie([50, 55]))

    assert round(f["retorno_iwm"], 4) == 0.1 and round(f["exceso_iwm"], 4) == 0.4


def test_sin_serie_del_sector_o_sin_precios_no_inventa_nada():
    assert ss.frente_al_sector(D0, _serie([10, 11]), []) is None
    assert ss.frente_al_sector(D0, [], _serie([100, 110])) is None
    assert ss.lectura_frente_al_sector(None) == "—"


def test_la_diferencia_pequena_cuenta_como_igual():
    assert ss.lectura_frente_al_sector(0.04) == "con su sector"
    assert ss.lectura_frente_al_sector(-0.04) == "con su sector"
    assert ss.lectura_frente_al_sector(0.06) == "mejor que su sector"


# ---------- la parte de base de datos ----------

class _Cursor:
    def __init__(self, respuestas):
        self.respuestas, self.sql = respuestas, ""

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return next(r for clave, r in self.respuestas.items() if clave in self.sql)

    def close(self):
        pass


class _Conn:
    def __init__(self, respuestas):
        self.respuestas = respuestas

    def cursor(self):
        return _Cursor(self.respuestas)

    def rollback(self):
        pass


def test_comparar_con_sectores_asigna_el_etf_por_el_sic_y_mide_cada_empresa():
    # SIC 2836 = biotecnología -> XBI
    conn = _Conn({
        "from empresas": [(1, "2836")],
        "from sector_precios": [("XBI", D0, 100.0), ("XBI", D0 + timedelta(days=1), 110.0)],
    })
    cartera = [{"empresa_id": 1, "ticker": "ABC", "fecha": D0}]
    precios = {"ABC": _serie([10, 20])}

    ss.comparar_con_sectores(conn, cartera, precios, bench=[])

    c = cartera[0]
    assert c["etf"] == "XBI" and c["sector"] != "otros"
    assert round(c["vs_sector"]["exceso_sector"], 4) == 0.9


def test_si_la_tabla_de_sectores_no_existe_la_cartera_sigue_sin_comparacion():
    class ConnSinTabla(_Conn):
        def cursor(self):
            c = _Cursor(self.respuestas)
            original = c.execute

            def execute(sql, params=None):
                if "sector_precios" in sql:
                    raise RuntimeError("relation sector_precios does not exist")
                original(sql, params)

            c.execute = execute
            return c

    conn = ConnSinTabla({"from empresas": [(1, "2836")]})
    cartera = [{"empresa_id": 1, "ticker": "ABC", "fecha": D0}]

    ss.comparar_con_sectores(conn, cartera, {"ABC": _serie([10, 20])}, bench=[])

    assert cartera[0]["vs_sector"] is None
