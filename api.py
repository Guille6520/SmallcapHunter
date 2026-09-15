"""
api.py — interfaz HTTP de SmallCap Hunter con FastAPI.

No sustituye al dashboard de Streamlit (dashboard.py) — es una segunda
interfaz sobre los MISMOS datos, pensada para consumo por programa (un
frontend propio, un script externo, un cron con curl) en vez de por un
humano frente a un navegador. Reutilizo la misma capa de acceso a datos
que ya usan detective.py y dashboard.py (obtener_contexto_empresa,
DB_CONFIG) en vez de duplicar consultas SQL.

Endpoints:
  GET  /health                       — vivo/muerto, para healthchecks
  GET  /candidatas                   — ranking de Capa 2 (igual que el
                                        panorama del dashboard)
  GET  /candidatas/{ticker}          — ficha completa de una empresa
                                        (scores, insiders, MD&A, veredictos)
  GET  /candidatas/{ticker}/similares — casos históricos por RAG (Capa 4)
  POST /candidatas/{ticker}/analizar  — dispara la Capa 3 completa
                                        (grafo de LangGraph) para este ticker

Cómo levantarla:
  uvicorn api:app --reload --port 8000

Documentación interactiva automática (Swagger) una vez levantada:
  http://127.0.0.1:8000/docs
"""

import os
import json
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

from detective import (
    obtener_contexto_empresa, buscar_casos_similares_rag,
    formatear_casos_similares_rag, modelo_secundario,
)
from grafo_capa3 import ejecutar_grafo_capa3

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

app = FastAPI(
    title="SmallCap Hunter API",
    description="Detección de small caps pre-explosivas sobre datos públicos de la SEC.",
    version="1.0.0",
)


@contextmanager
def conexion():
    """
    Una conexión de solo lectura por request. No reutilizo un pool
    global a propósito: es una API de bajo tráfico (uso personal /
    portafolio, no un producto con miles de usuarios concurrentes), así
    que la simplicidad de abrir y cerrar por request vale más que la
    complejidad de gestionar un pool para una carga que nunca la va a
    necesitar.
    """
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


class CandidataResumen(BaseModel):
    ticker: str
    nombre: str
    sector: Optional[str] = None
    market_cap_usd: Optional[int] = None
    score_total: Optional[int] = None
    veredicto: Optional[str] = None


class CasoSimilar(BaseModel):
    ticker: str
    nombre: str
    anio_fiscal: int
    trimestre: int
    similitud_pct: float
    extracto: str


class AnalisisResumen(BaseModel):
    ticker: str
    detective_groq_ok: bool
    detective_secundario_ok: bool
    auditorias_guardadas: int


@app.get("/health")
def health():
    """
    Comprueba también la conexión a la BD, no solo que el proceso esté
    vivo — un healthcheck que solo dice "sí, el proceso de Python
    existe" no sirve para detectar el caso real (Postgres caído).
    """
    try:
        with conexion() as conn:
            cur = conn.cursor()
            cur.execute("select 1")
            cur.close()
        return {"status": "ok", "db": "ok"}
    except psycopg2.Error as e:
        raise HTTPException(status_code=503, detail=f"BD no disponible: {e}")


@app.get("/candidatas", response_model=list[CandidataResumen])
def listar_candidatas(limite: int = 50, solo_analizadas: bool = False):
    """
    Ranking de candidatas. Por defecto, el mismo ranking de Capa 2 que
    muestra el panorama del dashboard (score_total desc, sin veredicto
    LLM todavía). Con solo_analizadas=true, en cambio, devuelvo las que
    YA pasaron por el Detective/Auditor, con su veredicto más reciente.
    """
    limite = max(1, min(limite, 200))

    with conexion() as conn:
        cur = conn.cursor()
        try:
            if solo_analizadas:
                cur.execute(
                    """
                    select distinct on (e.ticker)
                        e.ticker, e.nombre, e.sector, e.market_cap_usd,
                        a.score_total, a.veredicto
                    from auditorias a
                    join empresas e on e.id = a.empresa_id
                    where a.veredicto is not null
                    order by e.ticker, a.fecha_analisis desc
                    limit %s
                    """,
                    (limite,),
                )
            else:
                cur.execute(
                    """
                    select e.ticker, e.nombre, e.sector, e.market_cap_usd,
                           a.score_total, null as veredicto
                    from auditorias a
                    join empresas e on e.id = a.empresa_id
                    where a.veredicto is null
                    order by a.score_total desc
                    limit %s
                    """,
                    (limite,),
                )
            filas = cur.fetchall()
        finally:
            cur.close()

    columnas = ["ticker", "nombre", "sector", "market_cap_usd", "score_total", "veredicto"]
    return [CandidataResumen(**dict(zip(columnas, fila))) for fila in filas]


@app.get("/candidatas/{ticker}")
def ficha_candidata(ticker: str):
    """
    La misma ficha que ve el Detective antes de analizar: scores,
    insiders, MD&A, eventos 8-K, señales de mercado — más los
    veredictos ya guardados en auditorias, que obtener_contexto_empresa
    no trae (esa función es el contexto de ENTRADA al LLM, no el
    historial de análisis ya hechos).
    """
    with conexion() as conn:
        contexto = obtener_contexto_empresa(conn, ticker.upper())
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")

        cur = conn.cursor()
        try:
            cur.execute(
                """
                select modelo_llm, veredicto, respuesta_llm, verificacion_citas, fecha_analisis
                from auditorias
                where empresa_id = %s and veredicto is not null
                order by fecha_analisis desc
                limit 10
                """,
                (contexto["empresa_id"],),
            )
            analisis = cur.fetchall()
        finally:
            cur.close()

    return {
        "ticker": contexto["ticker"],
        "nombre": contexto["nombre"],
        "sector": contexto["sector"],
        "market_cap_usd": contexto["market_cap"],
        "bolsa": contexto["bolsa"],
        "scores": contexto["scores"],
        "texto_mda_disponible": bool(contexto["texto_mda"]),
        "analisis": [
            {
                "modelo": modelo, "veredicto": veredicto,
                "respuesta": respuesta, "verificacion_citas": verificacion,
                "fecha": fecha.isoformat() if fecha else None,
            }
            for modelo, veredicto, respuesta, verificacion, fecha in analisis
        ],
    }


@app.get("/candidatas/{ticker}/similares", response_model=list[CasoSimilar])
def casos_similares(ticker: str, k: int = 3):
    """
    Expone directamente la Capa 4 (RAG): qué trimestres históricos de
    OTRAS empresas se parecen semánticamente al MD&A más reciente de
    esta candidata. Útil para depurar o demostrar el RAG sin tener que
    lanzar un análisis completo de Capa 3.
    """
    k = max(1, min(k, 10))

    with conexion() as conn:
        contexto = obtener_contexto_empresa(conn, ticker.upper())
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")
        if not contexto["texto_mda"]:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} no tiene texto_mda todavía — ejecuta ingesta_10q.py",
            )

        casos = buscar_casos_similares_rag(conn, contexto["empresa_id"], contexto["texto_mda"], k)

    return [
        CasoSimilar(
            ticker=c["ticker"], nombre=c["nombre"],
            anio_fiscal=c["anio_fiscal"], trimestre=c["trimestre"],
            similitud_pct=round((1 - c["distancia"]) * 100, 1),
            extracto=(c["texto_mda"] or "")[:500],
        )
        for c in casos
    ]


@app.post("/candidatas/{ticker}/analizar", response_model=AnalisisResumen)
def analizar_candidata(ticker: str, modelos: Optional[str] = None):
    """
    Dispara la Capa 3 completa (Detective x2 + Auditor cruzado) para
    este ticker, en proceso — invoca ejecutar_grafo_capa3 directamente,
    no como subproceso (eso es lo que hace orquestador.py, que sí
    necesita aislamiento de proceso; una request HTTP ya está aislada
    por naturaleza).

    Es una llamada SÍNCRONA y puede tardar 10-30s (dos LLMs + el
    verificador de citas) — para un volumen alto de peticiones esto
    debería ser una tarea en background con polling, pero para el uso
    real del proyecto (unas pocas empresas al día) no compensa la
    complejidad de una cola de tareas.
    """
    ticker = ticker.upper()
    lista_modelos = modelos.split(",") if modelos else ["groq", modelo_secundario()]

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        contexto = obtener_contexto_empresa(conn, ticker)
        if not contexto:
            raise HTTPException(status_code=404, detail=f"{ticker} no encontrado")
        if not contexto["texto_mda"]:
            raise HTTPException(
                status_code=422,
                detail=f"{ticker} no tiene texto_mda todavía — ejecuta ingesta_10q.py",
            )

        estado_final = ejecutar_grafo_capa3(conn, ticker, lista_modelos)
    finally:
        conn.close()

    return AnalisisResumen(
        ticker=ticker,
        detective_groq_ok=estado_final.get("resultado_groq") is not None,
        detective_secundario_ok=estado_final.get("resultado_secundario") is not None,
        auditorias_guardadas=len(estado_final.get("auditorias_guardadas") or []),
    )
