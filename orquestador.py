"""
orquestador.py — el sistema completo corriendo solo, de la ingesta al
Telegram, sin que nadie tenga que ir empresa por empresa.

Filosofía: los scripts de ingesta e ingesta/scoring NO se tocan — el
orquestador los ejecuta en el mismo orden que la guía de pruebas (es la
misma secuencia que harías a mano). La única pieza que sí cambia es la
fase de agentes: desde que existe grafo_capa3.py, la pasada de 48h ya
no encadena `detective.py` x2 + `auditor.py` como tres subprocesos
sueltos — invoca el grafo de LangGraph una vez por ticker (ver
ejecutar_grafo_paso más abajo), que corre los dos Detectives en
paralelo y el Auditor cruzado en fan-in. La lógica de cuotas (qué
modelo se retira del resto de la pasada) sigue viviendo aquí, no en el
grafo: el grafo no sabe nada de "pasadas", solo de un ticker.

La condición de disparo es lo importante. Re-analizar una empresa sin
datos nuevos quema cuota gratuita para producir el mismo veredicto,
así que el Detective solo se lanza si:
  - la empresa supera el corte de Capa 2, Y
  - nunca fue analizada, O hay datos nuevos desde el último análisis
    (un Form 4 de compra nuevo o un evento 8-K registrado después)

Protección de cuotas (Gemini gratuito tiene límite diario; Claude es de
pago y no tiene tope de cuota, pero sí cuesta dinero por llamada): tope
de empresas por pasada (max_analisis_por_pasada) y
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
import json
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
from verificador_citas import VERSION_PROTOCOLO

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

# Fallos seguidos de un mismo modelo antes de retirarlo del resto de la
# pasada. Un fallo suelto (un 503 de Gemini, un JSON mal formado de
# Nemotron) no es cuota agotada: en la pasada del 24-sep dos fallos
# sueltos sacaron a los dos modelos y 6 de 10 empresas quedaron sin tocar.
FALLOS_SEGUIDOS_PARA_RETIRAR = 3

# Reintentos de una auditoría que no llegó a guardarse: tras este número
# de llamadas fallidas del Auditor sobre un mismo ticker en los últimos 7
# días, dejo de reintentar. Sin tope, un ticker cuyo JSON sale siempre
# roto (o cuyo texto desborda el contexto) gastaría llamadas en cada pasada.
MAX_FALLOS_AUDITORIA_7D = 3


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


def ejecutar_grafo_paso(ticker: str, modelos: list) -> dict:
    """
    Como ejecutar_paso, pero para grafo_capa3.py: a diferencia de los
    demás pasos, necesito leer su resultado (qué Detective falló en
    concreto), no solo si el subproceso terminó bien — así
    modelos_caidos puede seguir retirando UN modelo del resto de la
    pasada en vez de asumir que fallo total = los dos.

    Sigo lanzándolo como subproceso (no como import) por la misma razón
    que ejecutar_paso: un fallo aquí no debe corromper el estado en
    memoria del orquestador. La única diferencia es que capturo stdout
    para parsear la línea RESULTADO_JSON que imprime grafo_capa3.py al
    terminar — el resto de su salida (logs de detective.py/auditor.py,
    que grafo_capa3.py hereda vía import) la reenvío tal cual.
    """
    comando = [sys.executable, "grafo_capa3.py", "--ticker", ticker, "--modelos", ",".join(modelos)]
    log.info(f">>> grafo capa3 {ticker}: modelos={modelos}")
    inicio = time.time()

    resultado = subprocess.run(comando, capture_output=True, text=True)
    minutos = (time.time() - inicio) / 60

    salida = resultado.stdout or ""
    for linea in salida.splitlines():
        print(linea)
    if resultado.stderr:
        for linea in resultado.stderr.splitlines():
            print(linea)

    resumen = None
    for linea in salida.splitlines():
        if linea.startswith("RESULTADO_JSON: "):
            try:
                resumen = json.loads(linea[len("RESULTADO_JSON: "):])
            except json.JSONDecodeError:
                pass

    if resultado.returncode != 0 or resumen is None:
        log.warning(
            f">>> grafo capa3 {ticker} FALLÓ (código {resultado.returncode}) "
            "o no devolvió resumen — asumo que fallaron todos los modelos pedidos"
        )
        exitos = {modelo: False for modelo in modelos}
        exitos["auditorias_guardadas"] = 0
        return exitos

    log.info(f">>> grafo capa3 {ticker} terminado en {minutos:.1f} min")
    exitos = {}
    if "claude" in modelos:
        exitos["claude"] = resumen.get("detective_claude_ok", False)
    if "gemini" in modelos:
        exitos["gemini"] = resumen.get("detective_gemini_ok", False)
    exitos["auditorias_guardadas"] = resumen.get("auditorias_guardadas", 0)
    return exitos


def empresas_que_necesitan_analisis(conn, corte: int, limite: int) -> list:
    """
    El corazón del orquestador: qué empresas merecen LLM en esta pasada,
    y qué modelos les faltan.

    La granularidad es POR MODELO, no por empresa. La lección vino de
    una pasada real: Gemini agotó su cuota diaria a mitad, varias
    empresas se quedaron solo con el voto del otro modelo, y la versión anterior
    (que miraba "¿hay algún análisis?") las habría dado por completas
    para siempre. Ahora cada modelo tiene su propia condición: le toca
    si nunca analizó la empresa o si hay datos nuevos (Form 4 de compra
    o evento 8-K ingerido) desde SU último análisis.

    Identifico los análisis del Detective por su firma en el JSON
    (veredicto_preliminar) — así no los confundo con las filas de
    scoring (veredicto NULL) ni con las del Auditor.

    También le toca si su último análisis es de un protocolo anterior
    (prompt y verificador de citas más antiguos, ver VERSION_PROTOCOLO):
    las filas sin marca cuentan como versión 1. Las filas viejas no se
    borran -- el análisis nuevo se guarda al lado y pasa a ser el último.

    Devuelvo [(empresa_id, ticker, score, [modelos_pendientes]), ...].
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            with detectives as (
                select distinct on (empresa_id, modelo_llm)
                       empresa_id, modelo_llm,
                       fecha_analisis as ultima,
                       coalesce((respuesta_llm->>'version_protocolo')::int, 1) as version
                from auditorias
                where veredicto is not null
                  and respuesta_llm ? 'veredicto_preliminar'
                order by empresa_id, modelo_llm, fecha_analisis desc
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
            cross join (values ('claude'), ('gemini')) as m(modelo)
            left join detectives d
              on d.empresa_id = c.id and d.modelo_llm = m.modelo
            where d.ultima is null
               or d.version < %s
               or exists (select 1 from eventos_8k ev
                          where ev.empresa_id = c.id
                            and ev.fecha_registro > d.ultima)
               or exists (select 1 from insider_transactions it
                          where it.empresa_id = c.id
                            and it.tipo_transaccion = 'P'
                            and it.fecha_registro > d.ultima)
            order by c.score desc, c.id
            """,
            (corte, VERSION_PROTOCOLO),
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


def auditorias_pendientes(conn, corte: int, limite: int) -> list:
    """
    Tickers cuyo último análisis de algún Detective sigue sin auditoría del
    modelo opuesto. Existen porque la auditoría puede fallar sola (JSON
    cortado, 503, cuota) después de que el Detective ya guardó su análisis,
    y la condición de disparo de empresas_que_necesitan_analisis solo mira
    si falta el Detective: sin esto, esa auditoría quedaba sin hacer para
    siempre (el 14-oct quedaron 11 así).

    Solo candidatas por encima del corte, solo análisis del protocolo
    vigente (auditar una fila que la próxima pasada va a rehacer es tirar
    la llamada), y solo si el Auditor no ha fallado ya
    MAX_FALLOS_AUDITORIA_7D veces con ese ticker en 7 días. Los 429 de
    cuota no cuentan para ese tope: no son culpa del ticker, y contarlos
    lo dejaría una semana sin reintentos por haber agotado la cuota diaria.

    Devuelvo [(ticker, [modelos_de_detective_sin_auditar]), ...].
    """
    cur = conn.cursor()
    try:
        cur.execute(
            """
            with ultimos as (
                select distinct on (empresa_id, modelo_llm)
                       id, empresa_id, modelo_llm,
                       coalesce((respuesta_llm->>'version_protocolo')::int, 1) as version
                from auditorias
                where veredicto is not null
                  and respuesta_llm ? 'veredicto_preliminar'
                  and modelo_llm in ('claude', 'gemini')
                order by empresa_id, modelo_llm, fecha_analisis desc
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
            select c.ticker, u.modelo_llm
            from candidatas c
            join ultimos u on u.empresa_id = c.id
            where u.version >= %s
              and not exists (
                      select 1 from auditorias au
                      where au.modelo_llm = case u.modelo_llm
                                              when 'claude' then 'gemini'
                                              else 'claude' end
                        and au.respuesta_llm->>'detective_auditoria_id' = u.id::text
                  )
              and (select count(*) from llm_calls l
                   where l.origen = 'auditor' and not l.exito
                     and coalesce(l.tipo_error, '') not like '429%%'
                     and l.ticker = c.ticker
                     and l.fecha > now() - interval '7 days') < %s
            order by c.score desc, c.id, u.modelo_llm
            """,
            (corte, VERSION_PROTOCOLO, MAX_FALLOS_AUDITORIA_7D),
        )
        filas = cur.fetchall()
    finally:
        cur.close()

    por_ticker = {}
    for ticker, modelo in filas:
        if ticker not in por_ticker and len(por_ticker) >= limite:
            continue
        por_ticker.setdefault(ticker, []).append(modelo)
    return list(por_ticker.items())


def fase_auditorias(config: dict) -> dict:
    """
    Reintenta las auditorías que se quedaron sin hacer (ver
    auditorias_pendientes). Lanza el grafo SIN Detectives (--modelos "")
    para que solo corra el Auditor sobre lo ya guardado: no se paga otro
    análisis para conseguir una auditoría.

    Si el Auditor no guarda nada FALLOS_SEGUIDOS_PARA_RETIRAR veces
    seguidas, paro la fase: lo normal es cuota agotada o proveedor caído,
    y seguir solo repetiría el mismo error.
    """
    conn = conectar_db()
    try:
        pendientes = auditorias_pendientes(conn, config["corte"], config["max_analisis"])
    finally:
        conn.close()

    if not pendientes:
        log.info("Fase de auditorías: ninguna auditoría pendiente")
        return {"auditadas": 0, "fallos": 0}

    log.info(f"Fase de auditorías: {len(pendientes)} empresas con auditorías pendientes")
    auditadas, fallos, seguidos = 0, 0, 0

    for ticker, modelos_detective in pendientes:
        log.info(f"--- Auditando {ticker} (análisis sin auditar de: {modelos_detective}) ---")
        exitos = ejecutar_grafo_paso(ticker, [])
        guardadas = exitos.get("auditorias_guardadas", 0)
        time.sleep(config["pausa_llm"])

        if guardadas:
            auditadas += guardadas
            seguidos = 0
            continue

        fallos += 1
        seguidos += 1
        if seguidos >= FALLOS_SEGUIDOS_PARA_RETIRAR:
            log.warning(
                f"{seguidos} empresas seguidas sin auditoría guardada — corto la fase "
                "(probable cuota agotada o caída del proveedor). La siguiente pasada la retomará."
            )
            break

    return {"auditadas": auditadas, "fallos": fallos}


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
    # día), reintentar con el siguiente ticker solo produce el mismo 429,
    # así que lo retiro del resto de la pasada -- pero solo tras
    # FALLOS_SEGUIDOS_PARA_RETIRAR fallos consecutivos de ese modelo (un
    # éxito reinicia la cuenta). Retirarlo al primer fallo, asumiendo cuota,
    # sacaba a un modelo entero de la pasada por un 503 suelto o un JSON
    # roto. Si caen los dos, corto la fase entera. Lo que quede pendiente
    # lo recogerá la siguiente pasada — para eso está la condición de
    # disparo por modelo.
    modelos_caidos = set()
    fallos_seguidos = {}
    analizadas, fallos = 0, 0

    for empresa_id, ticker, score, modelos_pendientes in pendientes:
        modelos_a_ejecutar = [m for m in modelos_pendientes if m not in modelos_caidos]
        if not modelos_a_ejecutar:
            log.warning(
                f"{ticker}: sus modelos pendientes ({modelos_pendientes}) están "
                f"retirados de esta pasada — lo dejo para la siguiente"
            )
            continue

        log.info(f"--- Analizando {ticker} (score {score}, faltan: {modelos_a_ejecutar}) ---")

        # Un solo paso: el grafo corre los Detectives pendientes en
        # paralelo y el Auditor cruzado en fan-in (ver grafo_capa3.py).
        # Antes esto eran 2-3 subprocesos con una pausa fija entre cada
        # uno; ahora es un subproceso que hace las tres cosas y yo solo
        # pauso una vez al terminar. La lógica de qué modelo se retira
        # de la pasada por fallos seguidos sigue viviendo aquí.
        exitos = ejecutar_grafo_paso(ticker, modelos_a_ejecutar)
        algun_exito = False

        for modelo in modelos_a_ejecutar:
            if exitos.get(modelo):
                algun_exito = True
                fallos_seguidos[modelo] = 0
            else:
                fallos_seguidos[modelo] = fallos_seguidos.get(modelo, 0) + 1
                if fallos_seguidos[modelo] >= FALLOS_SEGUIDOS_PARA_RETIRAR:
                    modelos_caidos.add(modelo)
                    log.warning(
                        f"Retiro a {modelo} del resto de la pasada: "
                        f"{fallos_seguidos[modelo]} fallos seguidos "
                        "(probable cuota agotada o caída del proveedor)"
                    )
                else:
                    log.warning(
                        f"{modelo} falló en {ticker} "
                        f"({fallos_seguidos[modelo]}/{FALLOS_SEGUIDOS_PARA_RETIRAR} "
                        "seguidos): lo mantengo en la pasada"
                    )

        time.sleep(config["pausa_llm"])

        if algun_exito:
            analizadas += 1
        else:
            fallos += 1

        if len(modelos_caidos) == 2:
            log.warning(
                "Los dos modelos están retirados de esta pasada — corto la "
                "fase de agentes. La siguiente pasada retomará lo pendiente."
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
    auditorias = fase_auditorias(config)

    minutos = (datetime.now() - inicio).total_seconds() / 60
    resumen = (
        f"🤖 SmallCap Hunter — pasada completa en {minutos:.0f} min\n"
        f"Empresas analizadas por los agentes: {resultado['analizadas']}"
        + (f" (con fallos: {resultado['fallos']})" if resultado["fallos"] else "")
        + (f"\nAuditorías recuperadas: {auditorias['auditadas']}" if auditorias["auditadas"] else "")
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
