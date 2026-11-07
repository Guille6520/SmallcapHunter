"""
interruptores.py — conmutadores del pipeline que se leen de la tabla
`configuracion`, para poder cambiarlos sin tocar código ni reiniciar nada.

Hoy hay uno: `auditor_activo`. El Auditor es la segunda mitad de la idea del
proyecto (nadie se fía de un solo modelo), así que viene ENCENDIDO: sin la clave en
la tabla, corre. Se apaga —para depurar o si el gasto aprieta— con

    insert into configuracion (clave, valor) values ('auditor_activo', '0')
    on conflict (clave) do update set valor = excluded.valor;

y se vuelve a encender con el mismo comando y '1'. Apagarlo no borra nada: el código
y los sellos ya guardados siguen ahí.

Cada auditoría gasta una llamada de modelo por análisis (Claude audita a Gemini y
Gemini audita a Claude), y las de Gemini cuentan para su cuota diaria gratuita.
"""

VALORES_VERDADEROS = ("1", "true", "si", "sí", "on", "yes")


def es_verdadero(valor) -> bool:
    """«1», «true», «sí»... en cualquier mayúscula; cualquier otra cosa, y la
    ausencia de valor, es falso."""
    return str(valor).strip().lower() in VALORES_VERDADEROS


def auditor_encendido(valor) -> bool:
    """El valor de la clave `auditor_activo`: sin clave (None) está encendido; con
    clave, solo si dice «1», «true», «sí»..."""
    return True if valor is None else es_verdadero(valor)


def auditor_activo(conn) -> bool:
    """¿Está encendido el Auditor? Encendido por defecto, pero si no puedo leer la
    configuración lo dejo apagado: preferible no gastar llamadas a ciegas."""
    try:
        cur = conn.cursor()
        try:
            cur.execute("select valor from configuracion where clave = 'auditor_activo'")
            fila = cur.fetchone()
        finally:
            cur.close()
    except Exception:
        return False
    return auditor_encendido(fila[0] if fila else None)
