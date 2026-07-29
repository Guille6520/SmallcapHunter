"""
dashboard.py — interfaz Streamlit de SmallCap Hunter

Tres vistas:
  - Panorama: el embudo completo (de miles de empresas a un puñado de
    candidatas) y los últimos veredictos de los agentes.
  - Empresa: la ficha de una candidata — scores desagregados, compras
    de insiders, evolución de revenue y margen, el MD&A real, y lo que
    dijeron el Detective y el Auditor.
  - Chat: conversación libre con un LLM que tiene delante el MISMO
    contexto que vio el Detective (scores, insiders, MD&A, veredictos).
    Para preguntarle cosas como "¿por qué el score temporal es tan
    bajo?" o "resúmeme los riesgos en dos frases".

El chat reutiliza obtener_contexto_empresa de detective.py — así lo que
el chat "sabe" de una empresa es exactamente lo que sabe el pipeline,
no una versión distinta. Nota honesta: el chat NO pasa por el
verificador de citas (es conversación, no análisis persistido), así que
sus respuestas pueden contener imprecisiones — el veredicto oficial es
el de la tabla auditorias, no lo que diga el chat.

Cómo lanzarlo:
  streamlit run dashboard.py
"""

import os
import json
import logging

import pandas as pd
import psycopg2
import streamlit as st

from detective import obtener_contexto_empresa, MAX_CARACTERES_MDA
from notificador_telegram import enviar_telegram
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

st.set_page_config(
    page_title="SmallCap Hunter",
    page_icon="🦄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Un poco de CSS para que no parezca el Streamlit por defecto de todos
# los tutoriales: tarjetas con borde suave, cabecera con degradado, y
# los veredictos con su color de semáforo.
st.markdown("""
<style>
    .cabecera {
        background: linear-gradient(90deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
        padding: 1.2rem 1.6rem;
        border-radius: 14px;
        margin-bottom: 1rem;
    }
    .cabecera h1 { color: #e94560; margin: 0; font-size: 1.9rem; }
    .cabecera p  { color: #cfd8e3; margin: 0.3rem 0 0 0; font-size: 0.95rem; }
    [data-testid="stMetric"] {
        background: rgba(151, 166, 195, 0.08);
        border: 1px solid rgba(151, 166, 195, 0.25);
        border-radius: 12px;
        padding: 0.8rem;
    }
    .veredicto-MUY_INTERESANTE  { color: #21bf73; font-weight: 700; }
    .veredicto-INTERESANTE      { color: #f0a500; font-weight: 700; }
    .veredicto-NADA_INTERESANTE { color: #e94560; font-weight: 700; }
    .veredicto-ALUCINACION      { color: #9b59b6; font-weight: 700; }
</style>
""", unsafe_allow_html=True)


# ---------- acceso a datos ----------

@st.cache_resource
def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True   # solo lecturas; sin transacciones colgadas
    return conn


@st.cache_data(ttl=120)
def query_df(sql: str, params: tuple = None) -> pd.DataFrame:
    """Query -> DataFrame, cacheada 2 minutos para no machacar la BD
    con cada rerun de Streamlit (que re-ejecuta el script entero)."""
    conn = conectar_db()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        columnas = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columnas)
    finally:
        cur.close()


def leer_corte(conn=None) -> int:
    df = query_df("select valor from configuracion where clave = 'score_minimo_llm'")
    return int(df.iloc[0]["valor"]) if not df.empty else 25


# ---------- llamadas LLM para el chat (texto libre, sin JSON) ----------

def chat_groq(mensajes: list) -> str:
    from groq import Groq
    cliente = Groq(api_key=os.environ["GROQ_API_KEY"])
    respuesta = cliente.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=mensajes,
        temperature=0.4,
    )
    return respuesta.choices[0].message.content


def chat_gemini(mensajes: list) -> str:
    # Gemini no usa el formato de roles de OpenAI — aplano la
    # conversación en un solo texto. Para un chat de consulta sobre
    # una empresa es más que suficiente.
    from google import genai
    cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
    plano = "\n\n".join(
        f"[{m['role'].upper()}]\n{m['content']}" for m in mensajes
    )
    respuesta = cliente.models.generate_content(
        model="gemini-2.5-flash",
        contents=plano + "\n\n[ASSISTANT]\n",
    )
    return respuesta.text


def construir_contexto_chat(conn, ticker: str) -> str:
    """
    El system prompt del chat: el mismo contexto que ve el Detective,
    más los veredictos ya emitidos. Con instrucción explícita de
    ceñirse a estos datos — el chat no tiene verificador de citas
    detrás, así que al menos le acoto la materia prima.
    """
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        return None

    sp, sc, st_, scat, stotal = contexto["scores"] or (0, 0, 0, 0, 0)

    insiders = "\n".join(
        f"- {n} ({c or 'cargo no especificado'}): ${imp:,.0f} el {f}"
        for n, c, f, imp in contexto["transacciones"] if imp is not None
    ) or "(sin detalle de importes)"

    veredictos = query_df(
        """
        select a.modelo_llm, a.veredicto, a.fecha_analisis, a.respuesta_llm
        from auditorias a join empresas e on e.id = a.empresa_id
        where e.ticker = %s and a.veredicto is not null
        order by a.fecha_analisis desc limit 6
        """,
        (ticker,)
    )
    resumen_veredictos = "\n".join(
        f"- {r.modelo_llm} ({r.fecha_analisis:%Y-%m-%d}): {r.veredicto}"
        for r in veredictos.itertuples()
    ) or "(todavía sin análisis LLM)"

    mda = (contexto["texto_mda"] or "(sin MD&A descargado)")[:MAX_CARACTERES_MDA]

    return f"""Eres el analista de SmallCap Hunter, un sistema que busca small caps en fase pre-explosiva. El usuario va a conversar contigo sobre UNA empresa concreta. Responde en español, breve y directo, y básate SOLO en los datos de abajo. Si algo no está en estos datos, di que no lo tienes — no lo inventes.

EMPRESA: {contexto['nombre']} ({ticker}) | Sector: {contexto['sector']} | Bolsa: {contexto['bolsa']} | Market cap: ${(contexto['market_cap'] or 0):,.0f}

SCORES CAPA 2 (0-10 cada uno): precio 52w={sp}, convicción insiders={sc}, aceleración={st_}, catalizador={scat}, TOTAL={stotal}/40

COMPRAS DE INSIDERS RECIENTES:
{insiders}

VEREDICTOS DE LOS AGENTES:
{resumen_veredictos}

MD&A DEL ÚLTIMO 10-Q (puede estar recortado):
\"\"\"{mda}\"\"\""""


# ---------- componentes de la interfaz ----------

def cabecera():
    st.markdown(
        '<div class="cabecera"><h1>🦄 SmallCap Hunter</h1>'
        '<p>Detección de small caps en fase pre-explosiva — '
        'insiders + aceleración + agentes LLM con citas verificadas</p></div>',
        unsafe_allow_html=True,
    )


def vista_panorama():
    corte = leer_corte()

    totales = query_df("""
        select
            count(*)                                            as total,
            count(*) FILTER (where activa)                      as activas,
            count(*) FILTER (where estado = 'filtros_ok')       as filtros_ok,
            count(*) FILTER (where estado = 'scoring_ok')       as scoring_ok,
            count(*) FILTER (where estado = 'analizada')        as analizadas
        from empresas
    """).iloc[0]

    candidatas_llm = query_df(
        """
        select count(distinct empresa_id) as n
        from auditorias where veredicto is null and score_total >= %s
        """, (corte,)
    ).iloc[0]["n"]

    seguras = query_df(
        "select count(distinct empresa_id) as n from auditorias where veredicto = 'MUY_INTERESANTE'"
    ).iloc[0]["n"]

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Empresas descubiertas", f"{totales.total:,}")
    c2.metric("Activas", f"{totales.activas:,}")
    c3.metric("Pasaron Capa 1", f"{totales.filtros_ok + totales.scoring_ok:,}")
    c4.metric(f"Candidatas LLM (≥{corte}/40)", f"{candidatas_llm:,}")
    c5.metric("Analizadas por agentes", f"{totales.analizadas:,}")
    c6.metric("Muy interesantes", f"{seguras:,}")

    st.divider()

    izq, der = st.columns([3, 2])

    with izq:
        st.subheader("Ranking Capa 2")
        ranking = query_df(
            """
            select e.ticker, e.nombre, e.sector, e.market_cap_usd,
                   a.score_precio as precio, a.score_conviccion as conviccion,
                   a.score_temporal as temporal, a.score_catalizador as catalizador,
                   a.score_total as total
            from auditorias a
            join empresas e on e.id = a.empresa_id
            where a.veredicto is null
            order by a.score_total desc
            limit 50
            """
        )
        if ranking.empty:
            st.info("Todavía no hay scoring. Ejecuta filtro_capa1.py y scorer_capa2.py.")
        else:
            ranking["market_cap_usd"] = ranking["market_cap_usd"].map(
                lambda v: f"${v/1e6:,.0f}M" if pd.notna(v) else "—"
            )
            st.dataframe(
                ranking,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "total": st.column_config.ProgressColumn(
                        "total /40", min_value=0, max_value=40, format="%d"
                    ),
                },
            )

    with der:
        st.subheader("Últimos veredictos de los agentes")
        veredictos = query_df(
            """
            select e.ticker, a.modelo_llm as modelo, a.veredicto,
                   a.fecha_analisis::DATE as fecha
            from auditorias a
            join empresas e on e.id = a.empresa_id
            where a.veredicto is not null
            order by a.fecha_analisis desc
            limit 20
            """
        )
        if veredictos.empty:
            st.info("Aún no hay análisis LLM. Ejecuta detective.py sobre alguna candidata.")
        else:
            for r in veredictos.itertuples():
                st.markdown(
                    f"**{r.ticker}** · {r.modelo} · {r.fecha} → "
                    f"<span class='veredicto-{r.veredicto}'>{r.veredicto}</span>",
                    unsafe_allow_html=True,
                )


def vista_empresa(ticker: str):
    conn = conectar_db()
    contexto = obtener_contexto_empresa(conn, ticker)
    if not contexto:
        st.error(f"{ticker} no está en la base de datos")
        return

    sp, sc, st_, scat, stotal = contexto["scores"] or (0, 0, 0, 0, 0)

    st.subheader(f"{contexto['nombre']} ({ticker})")
    st.caption(
        f"{contexto['sector'] or 'sector desconocido'} · {contexto['bolsa'] or '—'} · "
        f"market cap ${(contexto['market_cap'] or 0):,.0f}"
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Precio 52w", f"{sp}/10")
    m2.metric("Convicción", f"{sc}/10")
    m3.metric("Aceleración", f"{st_}/10")
    m4.metric("Catalizador", f"{scat}/10")
    m5.metric("TOTAL", f"{stotal}/40")

    izq, der = st.columns(2)

    with izq:
        st.markdown("##### Revenue trimestral")
        serie = query_df(
            """
            select anio_fiscal || ' Q' || trimestre as trimestre, revenue, gross_profit
            from metricas_trimestrales mt
            join empresas e on e.id = mt.empresa_id
            where e.ticker = %s and revenue is not null
            order by anio_fiscal, mt.trimestre
            """,
            (ticker,)
        )
        if serie.empty:
            st.info("Sin métricas trimestrales para esta empresa.")
        else:
            st.bar_chart(serie.set_index("trimestre")["revenue"])
            con_margen = serie.dropna(subset=["gross_profit"]).copy()
            if not con_margen.empty:
                con_margen["margen bruto %"] = (
                    100 * con_margen["gross_profit"] / con_margen["revenue"]
                ).round(1)
                st.markdown("##### Margen bruto (%)")
                st.line_chart(con_margen.set_index("trimestre")["margen bruto %"])

    with der:
        st.markdown("##### Compras de insiders (últimas 10)")
        compras = pd.DataFrame(
            contexto["transacciones"],
            columns=["insider", "cargo", "fecha", "importe"],
        )
        if compras.empty:
            st.info("Sin compras P registradas.")
        else:
            compras["importe"] = compras["importe"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            )
            st.dataframe(compras, use_container_width=True, hide_index=True)

    if contexto["texto_mda"]:
        anio, trim = contexto["mda_anio_trim"]
        with st.expander(f"MD&A del 10-Q ({anio} Q{trim}) — el texto que leen los agentes"):
            st.text(contexto["texto_mda"][:15000])

    st.divider()
    st.markdown("##### Análisis de los agentes")
    analisis = query_df(
        """
        select a.modelo_llm, a.veredicto, a.fecha_analisis,
               a.respuesta_llm, a.verificacion_citas
        from auditorias a join empresas e on e.id = a.empresa_id
        where e.ticker = %s and a.veredicto is not null
        order by a.fecha_analisis desc
        """,
        (ticker,)
    )
    if analisis.empty:
        st.info(
            f"Sin análisis LLM todavía. Lánzalo con: "
            f"`python detective.py --ticker {ticker} --modelo groq`"
        )
    for r in analisis.itertuples():
        # psycopg2 devuelve JSONB como dict; si viniera como texto, lo parseo
        resp = r.respuesta_llm if isinstance(r.respuesta_llm, dict) else json.loads(r.respuesta_llm or "{}")
        verif = r.verificacion_citas if isinstance(r.verificacion_citas, dict) else json.loads(r.verificacion_citas or "{}")
        es_auditor = "veredicto_auditor" in resp

        with st.container(border=True):
            st.markdown(
                f"**{'Auditor' if es_auditor else 'Detective'}** · {r.modelo_llm} · "
                f"{r.fecha_analisis:%Y-%m-%d %H:%M} → "
                f"<span class='veredicto-{r.veredicto}'>{r.veredicto}</span> · "
                f"citas verificadas {verif.get('citas_verificadas', '—')}/{verif.get('citas_totales', '—')}",
                unsafe_allow_html=True,
            )
            if es_auditor:
                st.write(resp.get("justificacion", ""))
                if resp.get("riesgos_omitidos_por_detective"):
                    st.write("Riesgos que el Detective no vio: " +
                             "; ".join(resp["riesgos_omitidos_por_detective"]))
                if resp.get("fortalezas_omitidas_por_detective"):
                    st.write("Fortalezas que el Detective no vio: " +
                             "; ".join(resp["fortalezas_omitidas_por_detective"]))
            else:
                if resp.get("catalizador_no_obvio"):
                    st.write(f"**Catalizador:** {resp['catalizador_no_obvio']}")
                if resp.get("tesis_inversion"):
                    st.write(f"**Tesis:** {resp['tesis_inversion']}")
                if resp.get("riesgos"):
                    st.write("**Riesgos:** " + "; ".join(resp["riesgos"]))


def vista_chat(ticker: str, modelo: str):
    st.subheader(f"💬 Chat sobre {ticker}")
    st.caption(
        "El chat ve lo mismo que el Detective: scores, insiders, MD&A y "
        "veredictos. No pasa por el verificador de citas — para el "
        "veredicto oficial, mira la ficha de la empresa."
    )

    # Historial por ticker: si cambio de empresa, empiezo conversación nueva
    clave = f"chat_{ticker}"
    if clave not in st.session_state:
        st.session_state[clave] = []

    conn = conectar_db()
    sistema = construir_contexto_chat(conn, ticker)
    if sistema is None:
        st.error(f"{ticker} no está en la base de datos")
        return

    for msg in st.session_state[clave]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    pregunta = st.chat_input("Pregunta lo que quieras sobre esta empresa...")
    if pregunta:
        st.session_state[clave].append({"role": "user", "content": pregunta})
        with st.chat_message("user"):
            st.write(pregunta)

        mensajes = (
            [{"role": "system", "content": sistema}]
            + st.session_state[clave]
        )
        with st.chat_message("assistant"):
            with st.spinner(f"Pensando ({modelo})..."):
                try:
                    if modelo == "groq":
                        respuesta = chat_groq(mensajes)
                    else:
                        respuesta = chat_gemini(mensajes)
                except Exception as e:
                    respuesta = f"Error llamando a {modelo}: {e}"
            st.write(respuesta)
        st.session_state[clave].append({"role": "assistant", "content": respuesta})


# ---------- estructura principal ----------

def main():
    cabecera()

    with st.sidebar:
        st.markdown("### Navegación")
        vista = st.radio(
            "Vista", ["📊 Panorama", "🔎 Empresa", "💬 Chat"],
            label_visibility="collapsed",
        )

        ticker = None
        modelo = "groq"
        if vista in ("🔎 Empresa", "💬 Chat"):
            # Ofrezco primero las candidatas con mejor score — que son
            # las que de verdad interesa mirar — pero cualquier empresa
            # de la BD se puede escribir a mano.
            interesantes = query_df(
                """
                select e.ticker
                from auditorias a join empresas e on e.id = a.empresa_id
                where a.veredicto is null
                order by a.score_total desc
                limit 200
                """
            )
            opciones = interesantes["ticker"].tolist() if not interesantes.empty else []
            ticker = st.selectbox(
                "Empresa (ordenadas por score)", opciones,
                index=0 if opciones else None,
                accept_new_options=True,
            )

        if vista == "💬 Chat":
            modelo = st.radio("Modelo del chat", ["groq", "gemini"], horizontal=True)

        st.divider()
        if st.button("Probar alerta de Telegram"):
            ok = enviar_telegram("🦄 SmallCap Hunter: prueba de alerta desde el dashboard")
            st.success("Enviada — mira tu Telegram") if ok else st.warning(
                "No se envió. ¿TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID están en el entorno?"
            )
        st.caption("Los avisos automáticos los mandan detective.py y auditor.py al terminar cada análisis.")

    if vista == "📊 Panorama":
        vista_panorama()
    elif vista == "🔎 Empresa" and ticker:
        vista_empresa(ticker)
    elif vista == "💬 Chat" and ticker:
        vista_chat(ticker, modelo)
    else:
        st.info("Selecciona una empresa en la barra lateral.")


if __name__ == "__main__":
    main()
