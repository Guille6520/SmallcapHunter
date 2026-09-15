"""
tests/test_embeddings.py — la Capa 4 (RAG) sin llamar a la API de
verdad. Sustituyo embeddings._cliente() por un doble de prueba que
devuelve vectores fijos, así el test es determinista, gratis y no
depende de tener GEMINI_API_KEY en el entorno de CI.
"""

import pytest

import embeddings


class _RespuestaFalsa:
    def __init__(self, valores):
        class _Embedding:
            def __init__(self, values):
                self.values = values
        self.embeddings = [_Embedding(valores)]


class _ClienteFalso:
    def __init__(self, valores_a_devolver):
        self._valores = valores_a_devolver
        self.ultima_llamada = None

        class _Modelos:
            def __init__(self, outer):
                self._outer = outer

            def embed_content(self, model, contents, config):
                self._outer.ultima_llamada = {
                    "model": model, "contents": contents, "config": config,
                }
                return _RespuestaFalsa(self._outer._valores)

        self.models = _Modelos(self)


def test_generar_embedding_documento_dimension_correcta(monkeypatch):
    vector_falso = [0.1] * embeddings.DIMENSION_EMBEDDING
    cliente_falso = _ClienteFalso(vector_falso)
    monkeypatch.setattr(embeddings, "_cliente", lambda: cliente_falso)

    resultado = embeddings.generar_embedding_documento("texto de prueba")

    assert resultado == vector_falso
    assert len(resultado) == 1536
    assert cliente_falso.ultima_llamada["config"].task_type == "RETRIEVAL_DOCUMENT"


def test_generar_embedding_consulta_usa_task_type_distinto(monkeypatch):
    vector_falso = [0.2] * embeddings.DIMENSION_EMBEDDING
    cliente_falso = _ClienteFalso(vector_falso)
    monkeypatch.setattr(embeddings, "_cliente", lambda: cliente_falso)

    embeddings.generar_embedding_consulta("¿qué empresas se parecen a esta?")

    assert cliente_falso.ultima_llamada["config"].task_type == "RETRIEVAL_QUERY"


def test_dimension_incorrecta_lanza_error_claro(monkeypatch):
    """
    Si el SDK cambiara de comportamiento y devolviera una dimensión
    distinta a la que pedí, prefiero un ValueError explícito aquí a una
    fila con un vector corrupto que rompe el índice ivfflat en
    silencio meses después.
    """
    vector_incorrecto = [0.1] * 999
    cliente_falso = _ClienteFalso(vector_incorrecto)
    monkeypatch.setattr(embeddings, "_cliente", lambda: cliente_falso)

    with pytest.raises(ValueError):
        embeddings.generar_embedding_documento("texto de prueba")


def test_embeber_texto_vacio_lanza_error():
    with pytest.raises(ValueError):
        embeddings.generar_embedding_documento("")
    with pytest.raises(ValueError):
        embeddings.generar_embedding_documento("   ")


def test_vector_a_literal_pgvector():
    literal = embeddings.vector_a_literal_pgvector([0.1, -0.2, 3.0])
    assert literal.startswith("[") and literal.endswith("]")
    assert literal == "[0.1,-0.2,3.0]"
