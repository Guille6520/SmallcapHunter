"""
orquestador.py — el sistema completo corriendo solo, de la ingesta al
Telegram, sin que nadie tenga que ir empresa por empresa.

Filosofía: los scripts existentes NO se tocan — el orquestador los
ejecuta en el mismo orden que la guía de pruebas (es la misma secuencia
que harías a mano) y añade la única pieza que faltaba: decidir QUÉ
empresas merecen gasto de LLM en esta pasada.

La condición de disparo es lo importante. Re-analizar una empresa sin
datos nuevos quema cuota gratuita para producir el mismo veredicto,
así que el Detective solo se lanza si:
  - la empresa supera el corte de Capa 2, Y
  - nunca fue analizada, O hay datos nuevos desde el último análisis
    (un Form 4 de compra nuevo o un evento 8-K registrado después)

Protección de cuotas (Groq y Gemini gratuitos tienen límites por minuto
y por día): tope de empresas por pasada (max_analisis_por_pasada) y
pausa entre análisis (pausa_llm_segundos), ambos en la tabla
configuracion. Si una pasada deja empresas pendientes, caerán en la
siguiente — el sistema es de 48h, no de tiempo real.

Cómo usarlo:
  # Una pasada completa ahora mismo:
  python orquestador.py

  # Solo la fase de agentes (si la ingesta ya está fresca):
  python orquestador.py --solo-agentes

  # Pasada completa + re-descarga XBRL de todas las activas (lenta,
  # solo tiene sentido tras cierre de trimestre):
  python orquestador.py --completo

  # Modo servicio: una pasada cada N horas (scheduler_horas de la
  # tabla configuracion, 48 por defecto), hasta que lo pares con Ctrl+C:
  python orquestador.py --daemon
"""

import os
import sys
import time
import logging
import argparse
import subprocess
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

# Cargo el .env de la carpeta si existe — así las claves no dependen de
# pegarlas a mano en cada sesión nueva de PowerShell.
load_dotenv()

from notificador_telegram import enviar_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     os.getenv("DB_PORT", "5432"),
    "dbname":   os.getenv("DB_NAME", "smallcap_hunter"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

# Con cuántos tickers de mercado se conforma una pasada normal. El
# refresh completo de ~10.000 tickers a 0.6s/ticker son casi 2 horas —
# eso es para el modo --completo, no para el goteo de cada 48h.
LIMITE_MERCADO_PASADA_NORMAL = 800


def conectar_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


def leer_configuracion_orquestador(conn) -> dict:
    cur = conn.cursor()
    try:
        cur.execute(
            "select clave, valor from configuracion where clave in "
            "('score_minimo_llm', 'max_analisis_por_pasada', "
            "'pausa_llm_segundos', 'scheduler_horas')"
        )
        valores = dict(cur.fetchall())
        return {
            "corte": int(valores.get("score_minimo_llm", 25)),
            "max_analisis": int(valores.get("max_analisis_por_pasada", 10)),
            "pausa_llm": int(valores.get("pausa_llm_segundos", 20)),
            "horas": int(valores.get("scheduler_horas", 48)),
        }
    finally:
        cur.close()


def ejecutar_paso(nombre: str, argumentos: list, critico: bool) -> bool:
    """
    Lanzo un script del pipeline como subproceso con el mismo Python
    que me está ejecutando a mí. Elegí subprocesos y no imports a
    propósito: cada script mantiene sus logs y su argparse tal cual,
    y un fallo en uno no corrompe el estado en memoria de los demás.
    """
    comando = [sys.executable] + argumentos
    log.info(f">>> {nombre}: {' '.join(argumentos)}")
    inicio = time.time()

    resultado = subprocess.run(comando)
    minutos = (time.time() - inicio) / 60

    if resultado.returncode != 0:
        if critico:
            log.error(f">>> {nombre} FALLÓ (código {resultado.returncode}) — aborto la pasada")
            return False
        log.warning(f">>> {nombre} falló (código {resultado.returncode}) — sigo, no es crítico")
        return True

    log.info(f">>> {nombre} terminado en {minutos:.1f} min")
    return True


def empresas_que_necesitan_analisis(conn, corte: int, limite: int) -> list:
    """
    El corazón del orquestador: qué empresas merecen LLM en esta pasada,
    y qué modelos les faltan.

    La granularidad es POR MODELO, no por empresa. La lección vino de
    una pasada real: Gemini agotó su cuota diaria a mitad, varias
    empresas se quedaron solo con el voto de Groq, y la versión anterior
    (que miraba "¿hay algún análisis?") las habría dado por completas
    para siempre. Ahora cada modelo tiene su propia condición: le toca
    si nunca analizó la empresa o si hay datos nuevos (Form 4 de compra
    o evento 8-K ingerido) desde SU último análisis.

    Identifico los análisis del Detective por su firma en el JSON
    (veredicto_preliminar) — así no los confundo con las filas de
    scoring (veredicto NULL) ni con las del Auditor.

    Devuelvo [(empresa_id, ticker, score, [modelos_pendientes]), ...].
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            with detectives as (
                select empresa_id, modelo_llm, max(fecha_analisis) as ultima
                from auditorias
                where veredicto is not null
                  and respuesta_llm ? 'veredicto_preliminar'
                group by empresa_id, modelo_llm
            ),
            candidatas as (
                select e.id, e.ticker, max(a.score_total) as score
                from empresas e
                join auditorias a
                  on a.empresa_id = e.id
                 and a.veredicto is null
                 and a.score_total >= %s
                group by e.id, e.ticker
            )
            select c.id, c.ticker, c.score, m.modelo
            from candidatas c
            cross join (values ('groq'), (%s)) as m(modelo)
            left join detectives d
              on d.empresa_id = c.id and d.modelo_llm = m.modelo
            where d.ultima is null
               or exists (select 1 from eventos_8k ev
                          where ev.empresa_id = c.id
                            and ev.fecha_registro > d.ultima)
               or exists (select 1 from insider_transactions it
                          where it.empresa_id = c.id
                            and it.tipo_transaccion = 'P'
                            and it.fecha_registro > d.ultima)
            order by c.score desc, c.id
            """,
            (corte, os.getenv("MODELO_SECUNDARIO", "gemini").strip().lower()),
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    # Agrupo por empresa conservando el orden por score, y aplico el
    # límite sobre EMPRESAS (no sobre filas modelo).
    resultado = []
    indice = {}
    for empresa_id, ticker, score, modelo in filas:
        if empresa_id not in indice:
            if len(resultado) >= limite:
                continue
            indice[empresa_id] = len(resultado)
            resultado.append((empresa_id, ticker, score, []))
        resultado[indice[empresa_id]][3].append(modelo)
    return resultado


def fase_agentes(config: dict) -> dict:
    """
    Detective (dos votos ciegos) + Auditor cruzado para cada empresa
    que lo necesita, con pausas para no reventar las cuotas gratuitas.
    El Auditor ya salta por sí mismo lo que esté auditado — no necesito
    protegerlo desde aquí.
    """
    conn = conectar_db()
    try:
        pendientes = empresas_que_necesitan_analisis(
            conn, config["corte"], config["max_analisis"]
        )
    finally:
        conn.close()

    if not pendientes:
        log.info("Fase de agentes: ninguna empresa necesita análisis nuevo — nada que hacer")
        return {"analizadas": 0, "fallos": 0}

    log.info(
        f"Fase de agentes: {len(pendientes)} empresas pendientes "
        f"(tope por pasada: {config['max_analisis']})"
    )

    # Cuando un modelo agota su CUOTA DIARIA (Gemini free: 20 llamadas/
    # día; Groq: 100k tokens/día), reintentar con el siguiente ticker
    # solo produce el mismo 429. En cuanto un modelo falla, lo retiro
    # del resto de la pasada; si caen los dos, corto la fase entera.
    # Lo que quede pendiente lo recogerá la siguiente pasada — para eso
    # está la condición de disparo por modelo.
    modelos_caidos = set()
    analizadas, fallos = 0, 0

    for empresa_id, ticker, score, modelos_pendientes in pendientes:
        modelos_a_ejecutar = [m for m in modelos_pendientes if m not in modelos_caidos]
        if not modelos_a_ejecutar:
            log.warning(
                f"{ticker}: sus modelos pendientes ({modelos_pendientes}) están "
                f"sin cuota — lo dejo para la siguiente pasada"
            )
            continue

        log.info(f"--- Analizando {ticker} (score {score}, faltan: {modelos_a_ejecutar}) ---")
        algun_exito = False

        for modelo in modelos_a_ejecutar:
            ok = ejecutar_paso(
                f"detective {modelo} {ticker}",
                ["detective.py", "--ticker", ticker, "--modelo", modelo],
                critico=False,
            )
            if ok:
                algun_exito = True
            else:
                # Asumo cuota agotada. Es la causa abrumadoramente más
                # probable de un fallo aquí, y equivocarme solo cuesta
                # posponer ese modelo a la siguiente pasada.
                modelos_caidos.add(modelo)
                log.warning(f"Retiro a {modelo} del resto de la pasada (posible cuota diaria agotada)")
            time.sleep(config["pausa_llm"])

        # El auditor cruza modelos: solo tiene sentido si queda al menos
        # uno vivo para auditar. Él mismo salta lo ya auditado.
        if algun_exito and len(modelos_caidos) < 2:
            ejecutar_paso(
                f"auditor {ticker}", ["auditor.py", "--ticker", ticker], critico=False
            )
            time.sleep(config["pausa_llm"])

        if algun_exito:
            analizadas += 1
        else:
            fallos += 1

        if len(modelos_caidos) == 2:
            log.warning(
                "Los dos modelos están sin cuota — corto la fase de agentes. "
                "La siguiente pasada retomará lo pendiente."
            )
            break

    return {"analizadas": analizadas, "fallos": fallos}


def una_pasada(args) -> None:
    inicio = datetime.now()
    anio = inicio.year

    conn = conectar_db()
    try:
        config = leer_configuracion_orquestador(conn)
    finally:
        conn.close()

    if not args.solo_agentes:
        # La secuencia de la guía de pruebas, automatizada. El orden
        # importa: sin Form 4 nuevos no hay clusters nuevos; sin datos
        # de mercado la Capa 1 no filtra; sin scoring no hay candidatas
        # a las que bajarles los textos.
        pasos = [
            ("Form 4 (trimestre en curso)", ["loader_backfill.py", "--desde", str(anio), "--hasta", str(anio)], False),
        ]
        if args.completo:
            pasos.append(("XBRL (todas las activas)", ["enriquecedor_xbrl.py"], False))
            pasos.append(("Mercado (sin límite)", ["mercado_yfinance.py"], False))
        else:
            pasos.append(("Mercado (lote)", ["mercado_yfinance.py", "--limite", str(LIMITE_MERCADO_PASADA_NORMAL)], False))
        pasos += [
            ("Capa 1 (filtros)", ["filtro_capa1.py"], True),
            ("Capa 2 (scoring)", ["scorer_capa2.py"], True),
            ("MD&A 10-Q", ["ingesta_10q.py"], False),
            ("Eventos 8-K", ["ingesta_8k.py"], False),
            ("13D/G y shelf", ["ingesta_13dg.py"], False),
        ]

        for nombre, argumentos, critico in pasos:
            if not ejecutar_paso(nombre, argumentos, critico):
                return

    resultado = fase_agentes(config)

    minutos = (datetime.now() - inicio).total_seconds() / 60
    resumen = (
        f"🤖 SmallCap Hunter — pasada completa en {minutos:.0f} min\n"
        f"Empresas analizadas por los agentes: {resultado['analizadas']}"
        + (f" (con fallos: {resultado['fallos']})" if resultado["fallos"] else "")
    )
    log.info(resumen.replace("🤖 ", ""))
    # El detalle de cada veredicto ya llegó por Telegram desde los
    # propios agentes — esto es solo el cierre de la pasada.
    enviar_telegram(resumen)


def main():
    parser = argparse.ArgumentParser(
        description="Orquestador: pipeline completo + agentes solo donde hay datos nuevos"
    )
    parser.add_argument("--solo-agentes", action="store_true",
                        help="Salta la ingesta y va directo a la fase LLM")
    parser.add_argument("--completo", action="store_true",
                        help="Incluye XBRL completo y mercado sin límite (lento, para cierre de trimestre)")
    parser.add_argument("--daemon", action="store_true",
                        help="Repite la pasada cada scheduler_horas (48 por defecto) hasta Ctrl+C")
    args = parser.parse_args()

    if not args.daemon:
        una_pasada(args)
        return

    while True:
        una_pasada(args)

        conn = conectar_db()
        try:
            horas = leer_configuracion_orquestador(conn)["horas"]
        finally:
            conn.close()

        log.info(f"Modo daemon: próxima pasada en {horas}h (Ctrl+C para parar)")
        try:
            time.sleep(horas * 3600)
        except KeyboardInterrupt:
            log.info("Parado por el usuario")
            return


if __name__ == "__main__":
    main()
