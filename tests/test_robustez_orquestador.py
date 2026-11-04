"""
tests/test_robustez_orquestador.py — que el orquestador no se quede colgado ni
muera en silencio, que el aviso de Telegram no se pierda por un «<» ni deje el
token en el log, y que la API no acepte modelos inventados ni cuente detalles de
la base de datos. Todo simulado: sin subprocesos reales, sin red, sin BD.
"""

import logging
import subprocess
from contextlib import contextmanager

import psycopg2
import pytest
import requests
from fastapi.testclient import TestClient

import api
import notificador_telegram
import orquestador


# ---------- subprocesos con tiempo máximo ----------

def test_un_paso_colgado_se_mata_y_cuenta_como_fallo_no_critico(monkeypatch):
    def cuelga(comando, timeout=None, **kw):
        assert timeout == orquestador.TIMEOUT_PASO_SEGUNDOS, "todo subproceso lleva tiempo máximo"
        raise subprocess.TimeoutExpired(comando, timeout)

    monkeypatch.setattr(orquestador.subprocess, "run", cuelga)

    assert orquestador.ejecutar_paso("Mercado", ["mercado_yfinance.py"], critico=False) is True


def test_un_paso_critico_colgado_aborta_la_pasada(monkeypatch):
    def cuelga(comando, timeout=None, **kw):
        raise subprocess.TimeoutExpired(comando, timeout)

    monkeypatch.setattr(orquestador.subprocess, "run", cuelga)

    assert orquestador.ejecutar_paso("Capa 2", ["scorer_capa2.py"], critico=True) is False


def test_un_grafo_colgado_cuenta_como_fallo_de_todos_los_modelos_pedidos(monkeypatch):
    def cuelga(comando, timeout=None, **kw):
        assert timeout == orquestador.TIMEOUT_GRAFO_SEGUNDOS
        raise subprocess.TimeoutExpired(comando, timeout)

    monkeypatch.setattr(orquestador.subprocess, "run", cuelga)

    r = orquestador.ejecutar_grafo_paso("FAKE", ["claude", "gemini"])

    assert r["claude"] is False and r["gemini"] is False and r["auditorias_guardadas"] == 0


def test_los_tiempos_maximos_son_holgados_pero_finitos():
    assert 3600 <= orquestador.TIMEOUT_PASO_SEGUNDOS <= 12 * 3600
    assert 300 <= orquestador.TIMEOUT_GRAFO_SEGUNDOS <= 3600


# ---------- aviso si se aborta ----------

def test_un_paso_critico_que_falla_avisa_por_telegram(monkeypatch):
    avisos = []
    monkeypatch.setattr(orquestador, "conectar_db", lambda: _ConexionConfig())
    monkeypatch.setattr(orquestador, "ejecutar_paso", lambda nombre, args, critico: not critico)
    monkeypatch.setattr(orquestador, "enviar_telegram", lambda texto: avisos.append(texto))
    monkeypatch.setattr(orquestador, "fase_agentes", lambda *a, **k: pytest.fail("no debe seguir tras abortar"))

    class Args:
        solo_agentes = False
        completo = False

    orquestador.una_pasada(Args())

    assert len(avisos) == 1 and "abortó" in avisos[0]


class _ConexionConfig:
    def cursor(self):
        return self

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return []

    def close(self):
        pass


# ---------- daemon ----------

def test_el_daemon_sobrevive_a_una_pasada_que_revienta(monkeypatch):
    avisos, pasadas = [], []

    def pasada(args):
        pasadas.append(1)
        if len(pasadas) == 1:
            raise RuntimeError("algo inesperado")

    def duerme(segundos):
        if len(pasadas) >= 2:
            raise KeyboardInterrupt   # la segunda vuelta para el bucle
        # la primera espera "pasa" sin dormir de verdad

    monkeypatch.setattr(orquestador, "una_pasada", pasada)
    monkeypatch.setattr(orquestador, "conectar_db", lambda: _ConexionHoras())
    monkeypatch.setattr(orquestador, "enviar_telegram", lambda texto: avisos.append(texto))
    monkeypatch.setattr(orquestador.time, "sleep", duerme)
    monkeypatch.setattr("sys.argv", ["orquestador.py", "--daemon"])

    orquestador.main()

    assert len(pasadas) == 2, "tras la excepción, el servicio sigue y hace otra pasada"
    assert len(avisos) == 1 and "RuntimeError" in avisos[0]


class _ConexionHoras(_ConexionConfig):
    def fetchall(self):
        return [("scheduler_horas", "48")]


# ---------- Telegram ----------

def test_el_nombre_y_el_texto_del_modelo_se_escapan_en_el_aviso(monkeypatch):
    enviados = []
    monkeypatch.setattr(notificador_telegram, "enviar_telegram", lambda texto: enviados.append(texto) or True)

    notificador_telegram.notificar_analisis(
        agente="Detective", ticker="AT&T", nombre="Acme <Corp> & Sons", modelo="claude",
        veredicto="INTERESANTE", citas_verificadas=2, citas_totales=3,
        extra="el margen <5% & cae</b>",
    )

    texto = enviados[0]
    assert "Acme &lt;Corp&gt; &amp; Sons" in texto and "AT&amp;T" in texto
    assert "el margen &lt;5% &amp; cae&lt;/b&gt;" in texto
    assert texto.count("<b>") == 2 and texto.count("</b>") == 2, "solo las etiquetas propias"


def test_el_token_no_aparece_en_el_log_si_falla_el_envio(monkeypatch, caplog):
    token = "123456:SECRETO-del-bot"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def falla(url, **kw):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: /bot{token}/sendMessage")

    monkeypatch.setattr(notificador_telegram.requests, "post", falla)

    with caplog.at_level(logging.WARNING):
        assert notificador_telegram.enviar_telegram("hola") is False

    assert token not in caplog.text and "SECRETO" not in caplog.text
    assert "***" in caplog.text


# ---------- API ----------

@pytest.fixture
def cliente():
    return TestClient(api.app)


@pytest.mark.parametrize("modelos", ["gpt", "claude,otro", "groq", ",", " , "])
def test_la_api_rechaza_modelos_inventados_sin_tocar_la_base_de_datos(monkeypatch, cliente, modelos):
    monkeypatch.setattr(api.psycopg2, "connect", lambda **k: pytest.fail("no debe ni conectar"))
    monkeypatch.setattr(api, "ejecutar_grafo_capa3", lambda *a, **k: pytest.fail("no debe ejecutar el grafo"))

    r = cliente.post("/candidatas/FAKE/analizar", params={"modelos": modelos})

    assert r.status_code == 400 and "modelos no válidos" in r.json()["detail"]


def test_health_no_cuenta_detalles_de_la_base_de_datos(monkeypatch, cliente):
    @contextmanager
    def conexion_caida():
        raise psycopg2.OperationalError('connection to server at "10.0.0.5", port 5434 failed: user "postgres"')
        yield

    monkeypatch.setattr(api, "conexion", conexion_caida)

    r = cliente.get("/health")

    assert r.status_code == 503
    assert r.json()["detail"] == "BD no disponible"
    assert "10.0.0.5" not in r.text and "postgres" not in r.text
