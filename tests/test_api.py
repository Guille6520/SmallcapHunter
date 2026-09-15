"""
tests/test_api.py — los endpoints de api.py con TestClient de FastAPI,
sin BD real ni LLMs. Monkeypatcheo las funciones de acceso a datos que
api.py importa de detective.py/grafo_capa3.py, en vez de intentar
simular psycopg2 capa por capa — es más fiel a "qué contrato debe
cumplir el endpoint" que simular un cursor SQL genérico.
"""

from contextlib import contextmanager

import psycopg2
import pytest
from fastapi.testclient import TestClient

import api


class _FakeCursor:
    def __init__(self, filas=None):
        self._filas = filas or []

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._filas

    def close(self):
        pass


class _FakeConn:
    def __init__(self, filas_cursor=None):
        self._filas_cursor = filas_cursor or []
        self.autocommit = False

    def cursor(self):
        return _FakeCursor(self._filas_cursor)

    def close(self):
        pass


@pytest.fixture
def cliente():
    return TestClient(api.app)


def test_health_ok(monkeypatch, cliente):
    @contextmanager
    def fake_conexion():
        yield _FakeConn()

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_health_bd_caida_devuelve_503(monkeypatch, cliente):
    @contextmanager
    def fake_conexion():
        raise psycopg2.OperationalError("no puedo conectar")
        yield  # pragma: no cover

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/health")

    assert r.status_code == 503


def test_listar_candidatas(monkeypatch, cliente):
    filas = [
        ("NUVB", "Nuvation Bio", "Biotech", 900_000_000, 32, None),
        ("PLAY", "Dave & Buster's", "Consumer", 1_200_000_000, 28, None),
    ]

    @contextmanager
    def fake_conexion():
        yield _FakeConn(filas)

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/candidatas?limite=10")

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["ticker"] == "NUVB"
    assert body[0]["market_cap_usd"] == 900_000_000


def test_listar_candidatas_limite_se_acota(monkeypatch, cliente):
    """El endpoint no debe dejar pasar un ?limite=100000 tal cual a la
    consulta — lo acoto server-side."""
    capturado = {}

    @contextmanager
    def fake_conexion():
        conn = _FakeConn([])
        cursor_original = conn.cursor

        def cursor_que_espia():
            c = cursor_original()
            execute_original = c.execute

            def execute_espia(query, params=None):
                capturado["params"] = params
                return execute_original(query, params)

            c.execute = execute_espia
            return c

        conn.cursor = cursor_que_espia
        yield conn

    monkeypatch.setattr(api, "conexion", fake_conexion)

    cliente.get("/candidatas?limite=999999")

    assert capturado["params"][0] <= 200


def test_ficha_candidata_no_encontrada_devuelve_404(monkeypatch, cliente):
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: None)

    @contextmanager
    def fake_conexion():
        yield _FakeConn()

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/candidatas/NOEXISTE")

    assert r.status_code == 404


def test_ficha_candidata_encontrada(monkeypatch, cliente):
    contexto_falso = {
        "empresa_id": 1, "ticker": "NUVB", "nombre": "Nuvation Bio",
        "sector": "Biotech", "market_cap": 900_000_000, "bolsa": "NASDAQ",
        "scores": (7, 6, 8, 5, 26), "texto_mda": "MD&A de prueba",
    }
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: contexto_falso)

    @contextmanager
    def fake_conexion():
        yield _FakeConn([])  # sin auditorías previas guardadas

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/candidatas/nuvb")  # minúsculas -> debe normalizar a NUVB

    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NUVB"
    assert body["texto_mda_disponible"] is True
    assert body["analisis"] == []


def test_casos_similares_sin_texto_mda_devuelve_422(monkeypatch, cliente):
    contexto_sin_mda = {
        "empresa_id": 1, "ticker": "NUVB", "nombre": "Nuvation Bio",
        "texto_mda": None,
    }
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: contexto_sin_mda)

    @contextmanager
    def fake_conexion():
        yield _FakeConn()

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/candidatas/NUVB/similares")

    assert r.status_code == 422


def test_casos_similares_devuelve_ranking_por_similitud(monkeypatch, cliente):
    contexto_falso = {"empresa_id": 1, "ticker": "NUVB", "nombre": "Nuvation Bio", "texto_mda": "algo"}
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: contexto_falso)
    monkeypatch.setattr(
        api, "buscar_casos_similares_rag",
        lambda conn, empresa_id, texto_mda, k: [
            {"ticker": "BE", "nombre": "Bloom Energy", "anio_fiscal": 2018,
             "trimestre": 2, "texto_mda": "extracto", "distancia": 0.1},
        ],
    )

    @contextmanager
    def fake_conexion():
        yield _FakeConn()

    monkeypatch.setattr(api, "conexion", fake_conexion)

    r = cliente.get("/candidatas/NUVB/similares?k=3")

    assert r.status_code == 200
    body = r.json()
    assert body[0]["ticker"] == "BE"
    assert body[0]["similitud_pct"] == 90.0


def test_analizar_candidata_dispara_el_grafo(monkeypatch, cliente):
    contexto_falso = {"empresa_id": 1, "ticker": "NUVB", "texto_mda": "algo"}
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: contexto_falso)
    monkeypatch.setattr(api.psycopg2, "connect", lambda **kwargs: _FakeConn())

    llamada = {}

    def fake_ejecutar_grafo(conn, ticker, modelos):
        llamada["ticker"] = ticker
        llamada["modelos"] = modelos
        return {
            "resultado_groq": {"ok": True},
            "resultado_secundario": None,
            "auditorias_guardadas": [{"veredicto_final": "INTERESANTE"}],
        }

    monkeypatch.setattr(api, "ejecutar_grafo_capa3", fake_ejecutar_grafo)

    r = cliente.post("/candidatas/nuvb/analizar?modelos=groq")

    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NUVB"
    assert body["detective_groq_ok"] is True
    assert body["detective_secundario_ok"] is False
    assert body["auditorias_guardadas"] == 1
    assert llamada["modelos"] == ["groq"]


def test_analizar_candidata_no_encontrada_devuelve_404(monkeypatch, cliente):
    monkeypatch.setattr(api, "obtener_contexto_empresa", lambda conn, ticker: None)
    monkeypatch.setattr(api.psycopg2, "connect", lambda **kwargs: _FakeConn())

    r = cliente.post("/candidatas/NOEXISTE/analizar")

    assert r.status_code == 404
