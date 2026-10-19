"""
alertas.py — avisa por Telegram cuando el sistema se degrada, no solo cuando
termina un análisis.

La pestaña Observabilidad del dashboard ya enseña coste, errores y calidad
por modelo, pero hay que ir a mirarla. En sep-2026 el modelo gratuito que
ocupaba el hueco de Claude fue empeorando durante días (JSON roto, 429, 503)
y no se notó hasta que ya fallaba casi todo. Esto cierra ese hueco: una
revisión corta al final de cada pasada del orquestador, con un solo mensaje
y solo si hay algo que contar.

Está partido en dos mitades a propósito:
  - leer_metricas(conn): las consultas a la base, nada más.
  - evaluar(metricas): las reglas, puras y sin base de datos, para poder
    probarlas con diccionarios (tests/test_alertas.py).

Reglas (los umbrales están como constantes aquí abajo):
  - un modelo con demasiados errores en las últimas 48 h (la cuota diaria
    agotada no cuenta: tiene su propia alerta)
  - respuestas cortadas por el tope de tokens (la causa se arregla subiendo
    max_tokens, y es la que más tardamos en ver)
  - cuota gratuita de Gemini casi agotada o agotada hoy
  - gasto del día por encima de un tope
  - calidad de citas baja o análisis marcados ALUCINACION en la última semana

Cómo usarlo:
  python alertas.py            # evalúa y escribe las alertas, no envía nada
  python alertas.py --enviar   # además las manda por Telegram
El orquestador lo llama solo al final de cada pasada.
"""

import html
import logging
import argparse
from dataclasses import dataclass

import cuota_gemini
from errores_llm import clasificar_error
from verificador_citas import UMBRAL_CITAS_DEBILES

log = logging.getLogger(__name__)

VENTANA_ERRORES_HORAS = 48
# No juzgo la tasa de error de un modelo con menos llamadas que esto: con 3
# llamadas, un fallo suelto ya es el 33 %.
MIN_LLAMADAS_PARA_TASA = 5
# La línea base real (14-oct a 19-oct) fue ~10 % de llamadas fallidas por
# modelo, casi todo 503 puntuales y algún JSON roto: 25 % deja margen sin
# avisar por ruido, y está muy lejos del modelo gratuito que se degradó.
UMBRAL_TASA_ERROR = 0.25

# Cuota gratuita diaria de gemini-2.5-flash (se renueva a medianoche, hora
# del Pacífico). Si algún día se activa facturación, esta regla sobra.
LIMITE_DIARIO_GEMINI = cuota_gemini.LIMITE_DIARIO_POR_DEFECTO
AVISO_CUOTA_GEMINI = 18

UMBRAL_COSTE_DIARIO_USD = 1.00

VENTANA_CALIDAD_DIAS = 7
MIN_ANALISIS_PARA_CALIDAD = 3


@dataclass(frozen=True)
class Alerta:
    codigo: str       # estable, p. ej. "truncado_claude": sirve para tests y dedupe futuro
    gravedad: str     # "critica" | "aviso"
    mensaje: str


# ---------- las consultas ----------

def leer_metricas(conn) -> dict:
    """Todo lo que necesitan las reglas, leído de la base en una pasada.
    Las filas de llm_calls anteriores a la clasificación de errores se
    categorizan aquí al leerlas (clasificar_error es tolerante con ellas)."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select modelo, exito, tipo_error
            from llm_calls
            where fecha >= now() - make_interval(hours => %s)
            """,
            (VENTANA_ERRORES_HORAS,),
        )
        llamadas = {}
        for modelo, exito, tipo_error in cur.fetchall():
            m = llamadas.setdefault(modelo, {"total": 0, "fallos": 0, "por_categoria": {}})
            m["total"] += 1
            if not exito:
                m["fallos"] += 1
                categoria = clasificar_error(tipo_error)
                m["por_categoria"][categoria] = m["por_categoria"].get(categoria, 0) + 1

        # La cuenta de la cuota de Gemini de hoy vive en cuota_gemini.py (la
        # comparte con el orquestador y el dashboard).
        gemini_usadas_hoy = cuota_gemini.usadas_hoy(conn)
        gemini_429_hoy = cuota_gemini.hubo_429_hoy(conn)

        cur.execute(
            """
            select coalesce(sum(coste_estimado_usd), 0)
            from llm_calls where fecha::date = current_date
            """
        )
        coste_hoy = float(cur.fetchone()[0])

        # Solo análisis del protocolo con "% con soporte" (los anteriores no
        # lo tienen): es la métrica que decide la etiqueta ALUCINACION.
        cur.execute(
            """
            select modelo_llm,
                   count(*),
                   avg((verificacion_citas->>'porcentaje_soportado')::numeric),
                   count(*) filter (where veredicto = 'ALUCINACION')
            from auditorias
            where respuesta_llm ? 'veredicto_preliminar'
              and verificacion_citas ? 'porcentaje_soportado'
              and fecha_analisis >= now() - make_interval(days => %s)
            group by modelo_llm
            """,
            (VENTANA_CALIDAD_DIAS,),
        )
        calidad = {
            modelo: {"analisis": n, "pct_soportado": float(pct), "alucinaciones": alu}
            for modelo, n, pct, alu in cur.fetchall()
        }
    finally:
        cur.close()

    return {
        "llamadas": llamadas,
        "gemini_usadas_hoy": gemini_usadas_hoy,
        "gemini_429_hoy": gemini_429_hoy,
        "coste_hoy_usd": coste_hoy,
        "calidad": calidad,
    }


# ---------- las reglas ----------

def evaluar(metricas: dict) -> list:
    """De las métricas a la lista de alertas, sin tocar la base de datos."""
    alertas = []

    for modelo, m in sorted(metricas.get("llamadas", {}).items()):
        categorias = m["por_categoria"]

        truncadas = categorias.get("truncado", 0)
        if truncadas:
            alertas.append(Alerta(
                f"truncado_{modelo}", "critica",
                f"{modelo}: {truncadas} respuesta(s) cortadas por el tope de tokens. "
                f"Sube max_tokens (o acorta el prompt): el JSON sale incompleto y se pierde el análisis."
            ))

        # La cuota agotada tiene su propia alerta; contarla aquí la
        # dispararía cada día en cuanto Gemini llega a su límite.
        fallos = m["fallos"] - categorias.get("cuota_429", 0)
        total = m["total"] - categorias.get("cuota_429", 0)
        if total >= MIN_LLAMADAS_PARA_TASA and fallos / total > UMBRAL_TASA_ERROR:
            principal = max(
                ((c, n) for c, n in categorias.items() if c != "cuota_429"),
                key=lambda cn: cn[1], default=("desconocido", 0),
            )
            alertas.append(Alerta(
                f"errores_{modelo}", "critica",
                f"{modelo}: fallaron {fallos} de {total} llamadas en las últimas "
                f"{VENTANA_ERRORES_HORAS} h ({100 * fallos / total:.0f} %). "
                f"Lo más frecuente: {principal[0]} ({principal[1]})."
            ))

    if metricas.get("gemini_429_hoy", 0) > 0:
        alertas.append(Alerta(
            "cuota_gemini_agotada", "aviso",
            f"Gemini: cuota gratuita agotada hoy ({metricas.get('gemini_usadas_hoy', 0)} "
            f"llamadas usadas, límite {LIMITE_DIARIO_GEMINI}). Lo pendiente espera a que se renueve."
        ))
    elif metricas.get("gemini_usadas_hoy", 0) >= AVISO_CUOTA_GEMINI:
        alertas.append(Alerta(
            "cuota_gemini_casi", "aviso",
            f"Gemini: {metricas['gemini_usadas_hoy']} de {LIMITE_DIARIO_GEMINI} llamadas gratuitas usadas hoy."
        ))

    coste = metricas.get("coste_hoy_usd", 0.0)
    if coste > UMBRAL_COSTE_DIARIO_USD:
        alertas.append(Alerta(
            "coste_diario", "aviso",
            f"Gasto de hoy: ${coste:.2f} (tope de aviso ${UMBRAL_COSTE_DIARIO_USD:.2f})."
        ))

    for modelo, c in sorted(metricas.get("calidad", {}).items()):
        if c["analisis"] < MIN_ANALISIS_PARA_CALIDAD:
            continue
        if c["pct_soportado"] < UMBRAL_CITAS_DEBILES:
            alertas.append(Alerta(
                f"calidad_{modelo}", "aviso",
                f"{modelo}: solo el {c['pct_soportado']:.0f} % de sus citas tiene soporte en el texto "
                f"({c['analisis']} análisis en {VENTANA_CALIDAD_DIAS} días; mínimo {UMBRAL_CITAS_DEBILES} %)."
            ))
        if c["alucinaciones"]:
            alertas.append(Alerta(
                f"alucinacion_{modelo}", "aviso",
                f"{modelo}: {c['alucinaciones']} análisis marcados ALUCINACION en {VENTANA_CALIDAD_DIAS} días."
            ))

    return alertas


def formatear(alertas: list) -> str:
    """El mensaje de Telegram: una línea por alerta, las críticas primero."""
    iconos = {"critica": "🔴", "aviso": "🟡"}
    ordenadas = sorted(alertas, key=lambda a: a.gravedad != "critica")
    cuerpo = "\n".join(f"{iconos[a.gravedad]} {a.mensaje}" for a in ordenadas)
    return f"⚠️ SmallCap Hunter — {len(alertas)} alerta(s)\n{cuerpo}"


def revisar_y_avisar(conn, enviar: bool = True) -> list:
    """Evalúa, lo deja en el log y, si hay algo, manda UN mensaje. Nunca
    lanza: una alerta que falla no puede tirar la pasada que la lanzó."""
    try:
        alertas = evaluar(leer_metricas(conn))
    except Exception as e:
        log.warning(f"No pude evaluar las alertas: {e}")
        return []

    if not alertas:
        log.info("Alertas: todo en orden")
        return []

    for a in alertas:
        log.warning(f"ALERTA [{a.gravedad}] {a.mensaje}")
    if enviar:
        from notificador_telegram import enviar_telegram
        # enviar_telegram manda con parse_mode=HTML: un "<" o un "&" en el texto
        # (p. ej. en el detalle de un error) haría que Telegram rechace el mensaje.
        enviar_telegram(html.escape(formatear(alertas), quote=False))
    return alertas


def main():
    parser = argparse.ArgumentParser(description="Revisa el estado del sistema y avisa si se degrada")
    parser.add_argument("--enviar", action="store_true", help="manda también las alertas por Telegram")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
    from orquestador import conectar_db
    conn = conectar_db()
    try:
        alertas = revisar_y_avisar(conn, enviar=args.enviar)
    finally:
        conn.close()
    if alertas:
        print(formatear(alertas))


if __name__ == "__main__":
    main()
