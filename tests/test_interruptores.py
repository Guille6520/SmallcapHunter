"""
tests/test_interruptores.py — el Auditor viene encendido y se puede apagar de verdad:
apagado, ni el grafo ni la fase de auditorías pendientes lo lanzan, y la cuota de
Gemini deja de reservarle llamadas. Todo simulado: sin base de datos ni subprocesos.
"""

import auditor
import cuota_gemini
import grafo_capa3
import orquestador
from interruptores import auditor_activo, auditor_encendido, es_verdadero


class _Cursor:
    def __init__(self, fila=None, falla=False):
        self.fila, self.falla = fila, falla

    def execute(self, sql, params=None):
        if self.falla:
            raise RuntimeError("la base no responde")

    def fetchone(self):
        return self.fila

    def close(self):
        pass


class _Conn:
    def __init__(self, **kw):
        self.kw = kw

    def cursor(self):
        return _Cursor(**self.kw)


def test_es_verdadero_acepta_las_formas_habituales_y_nada_mas():
    for v in ("1", "true", "TRUE", " sí ", "Si", "on", "yes"):
        assert es_verdadero(v), v
    for v in ("0", "false", "no", "", None, "apagado"):
        assert not es_verdadero(v), v


def test_sin_la_clave_en_configuracion_el_auditor_esta_encendido():
    assert auditor_activo(_Conn(fila=None)) is True
    assert auditor_encendido(None) is True


def test_con_la_clave_a_uno_esta_encendido_y_a_cero_apagado():
    assert auditor_activo(_Conn(fila=("1",))) is True
    assert auditor_activo(_Conn(fila=("0",))) is False
    assert auditor_encendido("false") is False and auditor_encendido("") is False


def test_si_no_se_puede_leer_la_configuracion_se_queda_apagado():
    assert auditor_activo(_Conn(falla=True)) is False
    assert auditor_activo(object()) is False


def test_el_orquestador_lee_el_interruptor_de_la_configuracion():
    class Cur:
        def __init__(self, valores):
            self.valores = valores

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchall(self):
            return list(self.valores.items())

        def close(self):
            pass

    class Conn:
        def __init__(self, valores):
            self.valores = valores

        def cursor(self):
            return Cur(self.valores)

    assert orquestador.leer_configuracion_orquestador(Conn({}))["auditor_activo"] is True
    assert orquestador.leer_configuracion_orquestador(Conn({"auditor_activo": "1"}))["auditor_activo"] is True
    assert orquestador.leer_configuracion_orquestador(Conn({"auditor_activo": "0"}))["auditor_activo"] is False


def test_apagado_el_grafo_no_audita(monkeypatch):
    monkeypatch.setattr(grafo_capa3, "auditor_activo", lambda conn: False)
    monkeypatch.setattr(auditor, "ejecutar_auditor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe auditar")))

    r = grafo_capa3.nodo_auditor({"conn": object(), "ticker": "FAKE", "fallo_claude": False, "fallo_gemini": False})

    assert r == {"auditorias_guardadas": []}


def test_apagado_la_fase_de_auditorias_no_toca_ni_la_base_ni_el_grafo(monkeypatch):
    monkeypatch.setattr(orquestador, "conectar_db", lambda: (_ for _ in ()).throw(AssertionError("no debe conectar")))
    monkeypatch.setattr(orquestador, "ejecutar_grafo_paso", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe lanzar")))

    assert orquestador.fase_auditorias({"auditor_activo": False}) == {"auditadas": 0, "fallos": 0}
    assert orquestador.fase_auditorias({}) == {"auditadas": 0, "fallos": 0}


def test_sin_auditor_una_empresa_gasta_una_llamada_de_gemini_menos():
    assert cuota_gemini.llamadas_gemini_de_una_empresa(["claude", "gemini"], con_auditor=True) == 2
    assert cuota_gemini.llamadas_gemini_de_una_empresa(["claude", "gemini"], con_auditor=False) == 1
    assert cuota_gemini.llamadas_gemini_de_una_empresa(["claude"], con_auditor=False) == 0
