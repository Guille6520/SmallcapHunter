"""
tests/test_auditorias_pendientes.py — reintento de auditorías que se
quedaron sin hacer, y selección de qué análisis audita el Auditor. La BD,
el grafo y los sleeps van simulados: pruebo la política (qué se reintenta,
cuándo se para) y que las consultas llevan los filtros que importan, no
las llamadas a los LLM.
"""

import auditor
import grafo_capa3
import orquestador
import detective
from verificador_citas import VERSION_PROTOCOLO

CONFIG = {"corte": 25, "max_analisis": 10, "pausa_llm": 0}


class _Cursor:
    def __init__(self, filas):
        self.filas = filas
        self.ejecutadas = []

    def execute(self, sql, params=None):
        self.ejecutadas.append((sql, params))

    def fetchall(self):
        return self.filas

    def close(self):
        pass


class _Conn:
    def __init__(self, filas=()):
        self.cur = _Cursor(list(filas))

    def cursor(self):
        return self.cur

    def close(self):
        pass


# ---------- qué auditorías se reintentan ----------

def test_auditorias_pendientes_agrupa_por_ticker_y_respeta_el_limite():
    conn = _Conn([("AAA", "claude"), ("AAA", "gemini"), ("BBB", "claude"), ("CCC", "gemini")])

    pendientes = orquestador.auditorias_pendientes(conn, corte=25, limite=2)

    assert pendientes == [("AAA", ["claude", "gemini"]), ("BBB", ["claude"])], \
        "el límite cuenta EMPRESAS, no filas, y conserva el orden por score"


def test_auditorias_pendientes_solo_protocolo_vigente_y_con_tope_de_fallos():
    conn = _Conn()

    orquestador.auditorias_pendientes(conn, corte=25, limite=10)

    sql, params = conn.cur.ejecutadas[0]
    assert params == (25, VERSION_PROTOCOLO, orquestador.MAX_FALLOS_AUDITORIA_7D)
    assert "u.version >=" in sql, "no auditar análisis que la próxima pasada va a rehacer"
    assert "respuesta_llm ? 'veredicto_preliminar'" in sql, "solo filas de Detective, no de Auditor"
    assert "origen = 'auditor'" in sql and "not l.exito" in sql


def test_los_detectives_de_protocolo_antiguo_cuentan_como_pendientes():
    conn = _Conn()

    orquestador.empresas_que_necesitan_analisis(conn, corte=25, limite=10)

    sql, params = conn.cur.ejecutadas[0]
    assert params == (25, VERSION_PROTOCOLO)
    assert "version_protocolo" in sql and "d.version <" in sql


# ---------- la fase de auditorías ----------

def _preparar_fase(monkeypatch, pendientes, guardadas_por_ticker):
    intentados = []

    monkeypatch.setattr(orquestador, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(orquestador, "auditorias_pendientes", lambda *a, **k: pendientes)

    def fake_grafo(ticker, modelos):
        intentados.append((ticker, modelos))
        return {"auditorias_guardadas": guardadas_por_ticker[ticker]}

    monkeypatch.setattr(orquestador, "ejecutar_grafo_paso", fake_grafo)
    monkeypatch.setattr(orquestador.time, "sleep", lambda s: None)
    return intentados


def test_fase_auditorias_lanza_el_grafo_sin_detectives(monkeypatch):
    intentados = _preparar_fase(monkeypatch, [("AAA", ["claude"]), ("BBB", ["gemini"])],
                                {"AAA": 1, "BBB": 2})

    resultado = orquestador.fase_auditorias(CONFIG)

    assert intentados == [("AAA", []), ("BBB", [])], "modelos=[] -> solo Auditor, nada de Detectives"
    assert resultado == {"auditadas": 3, "fallos": 0}


def test_fase_auditorias_para_tras_fallos_seguidos(monkeypatch):
    n = orquestador.FALLOS_SEGUIDOS_PARA_RETIRAR
    tickers = [f"T{i}" for i in range(n + 2)]
    intentados = _preparar_fase(monkeypatch, [(t, ["claude"]) for t in tickers],
                                {t: 0 for t in tickers})

    resultado = orquestador.fase_auditorias(CONFIG)

    assert [t for t, _ in intentados] == tickers[:n]
    assert resultado == {"auditadas": 0, "fallos": n}


def test_fase_auditorias_un_exito_reinicia_la_cuenta(monkeypatch):
    n = orquestador.FALLOS_SEGUIDOS_PARA_RETIRAR
    guardadas = [0] * (n - 1) + [1] + [0] * (n - 1)
    tickers = [f"T{i}" for i in range(len(guardadas))]
    intentados = _preparar_fase(monkeypatch, [(t, ["claude"]) for t in tickers],
                                dict(zip(tickers, guardadas)))

    orquestador.fase_auditorias(CONFIG)

    assert len(intentados) == len(tickers)


def test_fase_auditorias_sin_pendientes_no_hace_nada(monkeypatch):
    intentados = _preparar_fase(monkeypatch, [], {})

    assert orquestador.fase_auditorias(CONFIG) == {"auditadas": 0, "fallos": 0}
    assert intentados == []


# ---------- qué análisis audita el Auditor ----------

def test_el_auditor_solo_mira_la_firma_del_detective_y_el_ultimo_de_cada_modelo():
    conn = _Conn([(1, "claude", {}, {})])

    auditor.obtener_analisis_detective(conn, empresa_id=7)

    sql, params = conn.cur.ejecutadas[0]
    assert "respuesta_llm ? 'veredicto_preliminar'" in sql, \
        "sin esto el Auditor audita las filas de otros auditores"
    assert "distinct on (modelo_llm)" in sql and "fecha_analisis desc" in sql
    assert params == (7, ["claude", "gemini"]), "los 'groq' históricos no se auditan por defecto"


def test_el_auditor_puede_pedir_un_detective_concreto():
    conn = _Conn()

    auditor.obtener_analisis_detective(conn, empresa_id=7, modelo_detective="groq")

    assert conn.cur.ejecutadas[0][1] == (7, ["groq"])


# ---------- el grafo con --modelos "" ----------

def test_modelos_vacio_ejecuta_solo_el_auditor(monkeypatch):
    llamadas = {"detective": [], "auditor": 0}

    monkeypatch.setattr(detective, "conectar_db", lambda: _Conn())
    monkeypatch.setattr(detective, "ejecutar_detective",
                        lambda conn, t, m: llamadas["detective"].append(m))

    def fake_auditor(conn, ticker, modelo_detective=None):
        llamadas["auditor"] += 1
        return []

    monkeypatch.setattr(auditor, "ejecutar_auditor", fake_auditor)

    grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE", modelos_a_ejecutar=[])

    assert llamadas["detective"] == [], "ningún Detective debe correr"
    assert llamadas["auditor"] == 1, "el Auditor corre aunque no se haya lanzado ningún Detective"


def test_main_distingue_modelos_ausente_de_modelos_vacio(monkeypatch, capsys):
    recibidos = []

    def fake_grafo(conn, ticker, modelos):
        recibidos.append(modelos)
        return {}

    monkeypatch.setattr(grafo_capa3, "ejecutar_grafo_capa3", fake_grafo)
    monkeypatch.setattr(detective, "conectar_db", lambda: _Conn())

    for argv in (["x", "--ticker", "A"], ["x", "--ticker", "A", "--modelos", ""],
                 ["x", "--ticker", "A", "--modelos", "claude"]):
        monkeypatch.setattr("sys.argv", argv)
        grafo_capa3.main()

    assert recibidos == [None, [], ["claude"]]
