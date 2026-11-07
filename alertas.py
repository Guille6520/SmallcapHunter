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
  - ingesta parada: una fuente (Form 4, mercado, 8-K, 13D/G, 10-Q) que lleva
    demasiado sin traer datos nuevos, un paso de la pasada que falló o
    SEC_CONTACT_EMAIL sin configurar (la SEC bloquea sin un contacto real)

Cómo usarlo:
  python alertas.py            # evalúa y escribe las alertas, no envía nada
  python alertas.py --enviar   # además las manda por Telegram
El orquestador lo llama solo al final de cada pasada.
"""

import os
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

# Ingesta: días sin datos nuevos a partir de los cuales aviso. Cada umbral sale de
# cómo publica la fuente, no de un número redondo:
#  - Form 4: la SEC lo publica en lotes trimestrales, el último llega semanas
#    después de cerrar el trimestre. Se mide por la fecha de la transacción (no
#    por la de carga: el upsert no la toca al recargar) y 130 días es un trimestre
#    más el retraso normal; pasarse significa que se saltó uno entero.
#  - Mercado: cada pasada actualiza un lote, y el servicio pasa cada 48 h.
#  - 8-K y 13D/G: solo se ingieren para las candidatas que pasan el corte de Capa 2
#    (unas decenas), y llegan cada semana entre todas; 13D/G es más raro, de ahí el
#    margen mayor.
#  - 10-Q: se mide por el fin de periodo del último trimestre; con el plazo de
#    presentación (45 días) y un poco de margen salen unos 140 días.
FUENTES_INGESTA = {
    "form4":   ("Form 4 (compras de insiders)", 130),
    "mercado": ("datos de mercado", 7),
    "8k":      ("eventos 8-K", 21),
    "13dg":    ("13D/G y shelf", 45),
    "10q":     ("MD&A de los 10-Q", 140),
}


@dataclass(frozen=True)
class Alerta:
    codigo: str       # estable, p. ej. "truncado_claude": sirve para tests y dedupe futuro
    gravedad: str     # "critica" | "aviso"
    mensaje: str


# ---------- las consultas ----------

def _dias(cur, sql: str):
    """Días enteros que devuelve una consulta de un solo valor, o None si la
    tabla está vacía (sin ninguna fila no hay 'última' de la que contar)."""
    cur.execute(sql)
    dias = cur.fetchone()[0]
    return None if dias is None else int(dias)


def leer_ingesta(cur) -> dict:
    """Días desde lo más reciente que trajo cada fuente de datos. La frescura se
    mide con el dato más nuevo que hay en la base, no con lo que dijo el script:
    un script puede terminar con código 0 habiendo traído cero filas."""
    return {
        "form4": _dias(cur, "select current_date - max(fecha_transaccion) "
                            "from insider_transactions where fecha_transaccion <= current_date"),
        "mercado": _dias(cur, "select extract(day from now() - max(ultimo_update)) from empresas where activa"),
        "8k": _dias(cur, "select extract(day from now() - max(fecha_registro)) from eventos_8k"),
        "13dg": _dias(cur, "select extract(day from now() - max(fecha_registro)) from participaciones_activistas"),
        "10q": _dias(cur, "select current_date - max(fecha_fin) from metricas_trimestrales "
                          "where fecha_fin <= current_date"),
    }


def sec_contacto_configurado() -> bool:
    """La SEC pide un contacto real en el User-Agent y devuelve 403 si no lo hay;
    cada script de ingesta se traga ese error por empresa y sigue. Con el valor por
    defecto (el texto 'configura-...') el sistema 'funciona' sin traer nada."""
    contacto = os.getenv("SEC_CONTACT_EMAIL", "")
    return "@" in contacto and "configura" not in contacto


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

        ingesta = leer_ingesta(cur)
    finally:
        cur.close()

    return {
        "llamadas": llamadas,
        "gemini_usadas_hoy": gemini_usadas_hoy,
        "gemini_429_hoy": gemini_429_hoy,
        "coste_hoy_usd": coste_hoy,
        "calidad": calidad,
        "ingesta": ingesta,
        "sec_contacto_ok": sec_contacto_configurado(),
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

    if not metricas.get("sec_contacto_ok", True):
        alertas.append(Alerta(
            "sec_sin_contacto", "critica",
            "SEC_CONTACT_EMAIL sin configurar en el .env: la SEC rechaza (403) las peticiones sin "
            "un contacto real y la ingesta no trae nada, sin dar error."
        ))

    for clave, dias in sorted(metricas.get("ingesta", {}).items()):
        nombre, umbral = FUENTES_INGESTA[clave]
        if dias is None:
            alertas.append(Alerta(
                f"ingesta_{clave}_vacia", "critica",
                f"Ingesta de {nombre}: no hay ningún dato en la base."
            ))
        elif dias > umbral:
            alertas.append(Alerta(
                f"ingesta_{clave}_parada", "critica",
                f"Ingesta de {nombre}: lo más reciente es de hace {dias} días (aviso a partir de {umbral}). "
                f"Revisa el log de ese paso."
            ))

    for paso in metricas.get("pasos_fallidos", []):
        alertas.append(Alerta(
            f"paso_fallido_{paso}", "critica",
            f"El paso «{paso}» falló en esta pasada (o pasó del tiempo máximo y lo maté). "
            f"Los datos de esa fuente no se han actualizado."
        ))

    return alertas


def formatear(alertas: list) -> str:
    """El mensaje de Telegram: una línea por alerta, las críticas primero."""
    iconos = {"critica": "🔴", "aviso": "🟡"}
    ordenadas = sorted(alertas, key=lambda a: a.gravedad != "critica")
    cuerpo = "\n".join(f"{iconos[a.gravedad]} {a.mensaje}" for a in ordenadas)
    return f"⚠️ SmallCap Hunter — {len(alertas)} alerta(s)\n{cuerpo}"


def revisar_y_avisar(conn, enviar: bool = True, pasos_fallidos: list = None) -> list:
    """Evalúa, lo deja en el log y, si hay algo, manda UN mensaje. Nunca
    lanza: una alerta que falla no puede tirar la pasada que la lanzó.
    pasos_fallidos son los pasos de ingesta que el orquestador vio fallar en esta
    pasada: eso no está en la base, solo lo sabe él."""
    try:
        metricas = leer_metricas(conn)
        metricas["pasos_fallidos"] = list(pasos_fallidos or [])
        alertas = evaluar(metricas)
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
