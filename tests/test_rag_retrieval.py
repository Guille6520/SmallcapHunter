"""
tests/test_rag_retrieval.py — la recuperación RAG dentro de detective.py
(buscar_casos_similares_rag / formatear_casos_similares_rag), con la BD
y el embedding simulados. Cubre en particular la degradación elegante:
el Detective tiene que seguir funcionando si la Capa 4 no está lista
todavía (sin embeddings generados) o si la API de embeddings falla.
"""

import psycopg2
import pytest

import detective


class _CursorFalso:
    def __init__(self, filas=None, excepcion=None):
        self._filas = filas or []
        self._excepcion = excepcion
        self.ultima_query = None
        self.ultimos_params = None

    def execute(self, query, params=None):
        self.ultima_query = query
        self.ultimos_params = params
        if self._excepcion:
            raise self._excepcion

    def fetchall(self):
        return self._filas

    def close(self):
        pass


class _ConnFalsa:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollback_llamado = False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rollback_llamado = True


def test_sin_texto_mda_no_llama_a_nada(monkeypatch):
    llamado = {"veces": 0}
    monkeypatch.setattr(detective, "generar_embedding_consulta", lambda t: llamado.__setitem__("veces", 1))

    resultado = detective.buscar_casos_similares_rag(_ConnFalsa(_CursorFalso()), 1, "")

    assert resultado == []
    assert llamado["veces"] == 0


def test_fallo_de_embedding_degrada_a_lista_vacia(monkeypatch):
    """Sin GEMINI_API_KEY, o con la API caída, el Detective no debe
    romperse — solo se queda sin el bloque RAG."""
    def _falla(texto):
        raise RuntimeError("GEMINI_API_KEY no configurada")

    monkeypatch.setattr(detective, "generar_embedding_consulta", _falla)

    resultado = detective.buscar_casos_similares_rag(_ConnFalsa(_CursorFalso()), 1, "algo de texto")

    assert resultado == []


def test_sin_embeddings_generados_todavia_degrada_a_lista_vacia(monkeypatch):
    """La BD responde (la columna existe) pero no hay filas con
    embedding no-nulo aún — generar_embeddings_rag.py no se ha corrido."""
    monkeypatch.setattr(detective, "generar_embedding_consulta", lambda t: [0.1] * 1536)
    cursor = _CursorFalso(filas=[])

    resultado = detective.buscar_casos_similares_rag(_ConnFalsa(cursor), 1, "algo de texto")

    assert resultado == []
    assert "mt.embedding is not null" in cursor.ultima_query


def test_error_de_bd_degrada_a_lista_vacia_y_hace_rollback(monkeypatch):
    monkeypatch.setattr(detective, "generar_embedding_consulta", lambda t: [0.1] * 1536)
    cursor = _CursorFalso(excepcion=psycopg2.OperationalError("BD caída"))
    conn = _ConnFalsa(cursor)

    resultado = detective.buscar_casos_similares_rag(conn, 1, "algo de texto")

    assert resultado == []
    assert conn.rollback_llamado


def test_casos_encontrados_se_parsean_y_excluyen_la_propia_empresa(monkeypatch):
    monkeypatch.setattr(detective, "generar_embedding_consulta", lambda t: [0.1] * 1536)
    filas = [
        ("BE", "Bloom Energy", 2018, 2, "texto del MD&A de Bloom...", 0.12),
        ("PLUG", "Plug Power", 2019, 1, "texto del MD&A de Plug...", 0.31),
    ]
    cursor = _CursorFalso(filas=filas)
    conn = _ConnFalsa(cursor)

    resultado = detective.buscar_casos_similares_rag(conn, empresa_id=42, texto_mda="algo", k=2)

    assert len(resultado) == 2
    assert resultado[0]["ticker"] == "BE"
    assert resultado[0]["distancia"] == pytest.approx(0.12)
    # empresa_id != %s en la query, no filtrado en Python — compruebo
    # que el parámetro de exclusión es el de la empresa actual
    assert 42 in cursor.ultimos_params


def test_formatear_casos_similares_vacio_no_anade_nada():
    assert detective.formatear_casos_similares_rag([]) == ""


def test_formatear_casos_similares_incluye_aviso_de_no_citable():
    casos = [{
        "ticker": "BE", "nombre": "Bloom Energy", "anio_fiscal": 2018,
        "trimestre": 2, "texto_mda": "extracto de ejemplo", "distancia": 0.1,
    }]
    bloque = detective.formatear_casos_similares_rag(casos)

    assert "BE" in bloque
    assert "no son fuente citable" in bloque.lower()
    assert "90%" in bloque  # similitud = (1 - 0.1) * 100
