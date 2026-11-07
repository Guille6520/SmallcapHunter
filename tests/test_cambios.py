"""
tests/test_cambios.py — qué cambió desde la última pasada: entradas, salidas y
cambios de score entre dos fotos de las candidatas, y la foto del día (idempotente).
Cálculo puro con diccionarios; la base de datos, con un cursor falso.
"""

from datetime import date

import cambios


def test_entran_salen_suben_y_bajan():
    antes = {1: 30, 2: 28, 3: 26, 4: 27}
    ahora = {1: 34, 2: 22, 4: 27, 5: 31}

    c = cambios.comparar(antes, ahora)

    assert c["entran"] == [5]
    assert c["salen"] == [3]
    assert c["suben"] == [(1, 30, 34)]
    assert c["bajan"] == [(2, 28, 22)]


def test_un_cambio_de_score_por_debajo_del_umbral_no_cuenta():
    c = cambios.comparar({1: 30, 2: 30}, {1: 32, 2: 28})
    assert c["suben"] == [] and c["bajan"] == []

    c = cambios.comparar({1: 30}, {1: 33})
    assert c["suben"] == [(1, 30, 33)], "justo en el umbral cuenta"


def test_las_entradas_van_por_score_de_mayor_a_menor():
    c = cambios.comparar({}, {1: 26, 2: 33, 3: 29})
    assert c["entran"] == [2, 3, 1]


def test_listas_vacias_no_rompen_nada():
    assert cambios.comparar({}, {}) == {"entran": [], "salen": [], "suben": [], "bajan": []}


class _Cursor:
    def __init__(self, log, fechas=(), filas=()):
        self.log, self.fechas, self.filas, self.rowcount = log, list(fechas), list(filas), 7

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))
        self.ultima = sql

    def fetchall(self):
        if "distinct fecha" in self.ultima:
            return [(f,) for f in self.fechas]
        if "from candidatas_historial where fecha in" in self.ultima:
            return self.filas
        if "from empresas where id" in self.ultima:
            return [(1, "AAA", "Alfa"), (2, "BBB", "Beta"), (3, "CCC", "Gamma")]
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self, **kw):
        self.log, self.kw, self.commits = [], kw, 0

    def cursor(self):
        return _Cursor(self.log, **self.kw)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_la_foto_del_dia_borra_la_anterior_del_mismo_dia_y_la_rehace_en_una_transaccion():
    conn = _Conn()

    n = cambios.guardar_foto(conn, date(2026, 11, 6))

    sqls = [s for s, _ in conn.log]
    i_borrar = next(i for i, s in enumerate(sqls) if s.startswith("delete from candidatas_historial"))
    i_meter = next(i for i, s in enumerate(sqls) if s.startswith("insert into candidatas_historial"))
    assert i_borrar < i_meter and n == 7
    assert conn.commits >= 1


def test_con_una_sola_foto_se_dice_que_no_hay_historial_en_vez_de_inventar_cambios():
    conn = _Conn(fechas=[date(2026, 11, 6)])

    assert cambios.leer_cambios_de_lista(conn) == {"hay_historial": False}


def test_con_dos_fotos_devuelve_entradas_y_salidas_con_nombre():
    conn = _Conn(
        fechas=[date(2026, 11, 6), date(2026, 11, 3)],
        filas=[(date(2026, 11, 3), 1, 30), (date(2026, 11, 3), 2, 28),
               (date(2026, 11, 6), 1, 30), (date(2026, 11, 6), 3, 31)],
    )

    r = cambios.leer_cambios_de_lista(conn)

    assert r["hay_historial"] and r["desde"] == date(2026, 11, 3) and r["hasta"] == date(2026, 11, 6)
    assert r["entran"] == [("CCC", "Gamma", 31)]
    assert r["salen"] == [("BBB", "Beta", 28)]
    assert r["suben"] == [] and r["bajan"] == []


def test_la_fase_nunca_rompe_la_pasada(monkeypatch):
    import orquestador

    def sin_base():
        raise ConnectionError("sin base de datos")

    monkeypatch.setattr(orquestador, "conectar_db", sin_base)

    assert cambios.fase_cambios() == {"guardadas": 0}
