"""
cuota_gemini.py — cuánta cuota gratuita de Gemini llevamos gastada hoy y qué
cabe todavía en una pasada.

El tier gratuito de gemini-2.5-flash permite 20 peticiones al día (se
renueva a medianoche, hora del Pacífico) y el orquestador solo se enteraba
al recibir el 429. Con la cuenta a mano pasa a poder planificar: cada empresa
nueva cuesta unas 2 llamadas de Gemini (su Detective y la auditoría del
análisis de Claude), así que 20 llamadas son ~10 empresas, que es justo lo
que observamos pasada tras pasada. Se muestra en el dashboard y el
orquestador recorta la pasada a lo que cabe en vez de gastar llamadas que ya
sabe que van a fallar.

La cuenta sale de llm_calls, o sea solo de lo que registra este sistema: si
la misma clave se usa desde otro sitio, la cuota real será menor que la que
aquí se ve. Una petición que acaba en 429 o 503 no consume cuota, así que no
la cuento.

Si algún día se activa facturación en la cuenta de Gemini, el límite deja de
existir: configuracion.gemini_limite_diario = 0 desactiva todo esto.
"""

LIMITE_DIARIO_POR_DEFECTO = 20

# Prefijos de tipo_error de las peticiones que NO gastan cuota: las que el
# proveedor rechazó (429) o no llegó a atender (503, 504, 529). Incluye los
# textos antiguos sin categoría, que empezaban por el código HTTP.
SIN_CUOTA = "^(cuota_429|sobrecarga_503|429|50[34]|529)"

_DIA_PACIFICO = "(fecha at time zone 'America/Los_Angeles')::date = (now() at time zone 'America/Los_Angeles')::date"


def usadas_hoy(conn) -> int:
    """Peticiones a Gemini de hoy (día del Pacífico) que consumieron cuota."""
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            select count(*) from llm_calls
            where modelo = 'gemini'
              and {_DIA_PACIFICO}
              and (exito or coalesce(tipo_error, '') !~ %s)
            """,
            (SIN_CUOTA,),
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def hubo_429_hoy(conn) -> int:
    """Cuántas peticiones de hoy recibieron un 429 (cuota o límite por minuto)."""
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            select count(*) from llm_calls
            where modelo = 'gemini' and not exito
              and {_DIA_PACIFICO}
              and coalesce(tipo_error, '') ~ '^(cuota_429|429)'
            """
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def renovacion(conn):
    """Cuándo se renueva la cuota (próxima medianoche del Pacífico), en hora
    de Madrid. Lo calcula Postgres, que sí tiene la base de zonas horarias
    en cualquier sistema."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            select ((date_trunc('day', now() at time zone 'America/Los_Angeles') + interval '1 day')
                    at time zone 'America/Los_Angeles') at time zone 'Europe/Madrid'
            """
        )
        return cur.fetchone()[0]
    finally:
        cur.close()


def llamadas_gemini_de_una_empresa(modelos_pendientes: list) -> int:
    """Lo que va a gastar de cuota analizar una empresa con el grafo: su
    Detective de Gemini si le toca, y la auditoría de Gemini sobre el
    análisis de Claude (que existirá al terminar el Detective de Claude, o
    ya existe). La auditoría de Claude sobre Gemini no gasta cuota de Gemini."""
    return (1 if "gemini" in modelos_pendientes else 0) + 1


def llamadas_gemini_de_una_auditoria(modelos_detective: list) -> int:
    """Una auditoría pendiente gasta cuota de Gemini solo cuando es Gemini
    quien audita, o sea cuando el análisis pendiente de auditar es de Claude."""
    return modelos_detective.count("claude")


def recortar_por_cuota(pendientes: list, restantes: int, coste) -> tuple:
    """Recorre las pendientes en su orden (ya vienen por score) y se queda con
    las que caben en lo que queda de cuota. Una que no cabe no frena a las
    siguientes: si una más barata sí cabe, entra.

    pendientes: lista de tuplas; coste(tupla) -> llamadas de Gemini que gasta.
    Devuelve (las_que_caben, las_aplazadas)."""
    caben, aplazadas = [], []
    for item in pendientes:
        c = coste(item)
        if c <= restantes:
            caben.append(item)
            restantes -= c
        else:
            aplazadas.append(item)
    return caben, aplazadas
