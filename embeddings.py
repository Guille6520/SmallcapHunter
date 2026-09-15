"""
embeddings.py — Capa 4 (RAG): genero y busco embeddings semánticos del
texto narrativo (MD&A de 10-Q, eventos 8-K) para encontrar, dado un
trimestre nuevo, los trimestres históricos más parecidos en significado
— no en palabras clave — y dárselos al Detective como few-shot.

Uso Gemini (gemini-embedding-001) porque ya tengo GEMINI_API_KEY
configurada para la Capa 3 — no añado un proveedor nuevo solo para esto.
El SDK es el mismo cliente unificado (google-genai) que ya usa
detective.py para las llamadas de generación.

Dimensión: el schema fija vector(1536) en metricas_trimestrales.embedding
y eventos_8k.embedding (ver schema.sql). gemini-embedding-001 soporta
output_dimensionality configurable (representación Matryoshka), así que
pido explícitamente 1536 en vez de la dimensión nativa del modelo (3072)
— así el vector encaja en la columna sin tener que tocar el schema ni
recrear el índice ivfflat.

task_type importa en este modelo: un texto embebido como documento y el
mismo texto embebido como consulta NO producen el mismo vector, aunque
el contenido sea idéntico — el modelo optimiza cada uno para su papel
en la búsqueda. Por eso separo generar_embedding_documento (para lo que
guardo en la BD) de generar_embedding_consulta (para lo que busco).
"""

import os
import logging

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

MODELO_EMBEDDING = "gemini-embedding-001"
DIMENSION_EMBEDDING = 1536


def _cliente():
    from google import genai
    api_key = os.environ["GEMINI_API_KEY"].strip()
    return genai.Client(api_key=api_key)


def _embeber(texto: str, task_type: str) -> list:
    """
    Llamada de bajo nivel compartida. task_type es RETRIEVAL_DOCUMENT
    para texto que voy a guardar y buscar después, o RETRIEVAL_QUERY
    para el texto con el que estoy buscando en este momento.
    """
    from google.genai import types

    if not texto or not texto.strip():
        raise ValueError("No puedo embeber texto vacío")

    cliente = _cliente()
    respuesta = cliente.models.embed_content(
        model=MODELO_EMBEDDING,
        contents=texto,
        config=types.EmbedContentConfig(
            output_dimensionality=DIMENSION_EMBEDDING,
            task_type=task_type,
        ),
    )
    valores = list(respuesta.embeddings[0].values)

    if len(valores) != DIMENSION_EMBEDDING:
        # No debería pasar nunca con output_dimensionality fijado, pero
        # si Google cambia el comportamiento del SDK prefiero un error
        # claro aquí a una fila con un vector de dimensión incorrecta
        # que rompe el índice ivfflat en silencio.
        raise ValueError(
            f"Embedding de dimensión {len(valores)}, esperaba {DIMENSION_EMBEDDING}"
        )
    return valores


def generar_embedding_documento(texto: str) -> list:
    """Para texto que voy a guardar en metricas_trimestrales.embedding
    o eventos_8k.embedding — el lado 'corpus' de la búsqueda."""
    return _embeber(texto, "RETRIEVAL_DOCUMENT")


def generar_embedding_consulta(texto: str) -> list:
    """Para el texto con el que busco casos parecidos en este momento
    — el lado 'pregunta' de la búsqueda."""
    return _embeber(texto, "RETRIEVAL_QUERY")


def vector_a_literal_pgvector(valores: list) -> str:
    """
    psycopg2 no conoce el tipo `vector` de pgvector de forma nativa.
    En vez de añadir una dependencia extra (el paquete `pgvector` con su
    adaptador), lo cual son 2 líneas de registro pero una dependencia
    más para algo que no lo necesita: pgvector acepta directamente la
    representación de texto '[0.1,0.2,...]' en cualquier INSERT/UPDATE,
    así que basta con pasarla como un string normal.
    """
    return "[" + ",".join(repr(float(v)) for v in valores) + "]"
