"""
tests/test_alertas_ingesta.py — que una ingesta parada se note. Los scripts de
ingesta terminan con código 0 aunque no traigan nada (se tragan el error de cada
empresa), así que la frescura se mide con el dato más nuevo de la base, y el
umbral de cada fuente sale de cómo la publica. Reglas puras con diccionarios, la
consulta con un cursor falso y el orquestador sin subprocesos reales.
"""

import alertas
import orquestador
from alertas import FUENTES_INGESTA, evaluar, leer_ingesta, revisar_y_avisar


def _al_dia():
    return {clave: 1 for clave in FUENTES_INGESTA}


def _metricas(**kw):
    base = {
        "llamadas": {}, "gemini_usadas_hoy": 0, "gemini_429_hoy": 0,
        "coste_hoy_usd": 0.0, "calidad": {}, "ingesta": _al_dia(), "sec_contacto_ok": True,
    }
    base.update(kw)
    return base


def _codigos(m):
    return {a.codigo for a in evaluar(m)}


# ---------- las reglas ----------

def test_con_las_fuentes_al_dia_no_hay_alerta():
    assert evaluar(_metricas()) == []


def test_una_fuente_justo_en_su_umbral_no_avisa_y_un_dia_despues_si():
    en_el_limite = {clave: umbral for clave, (_, umbral) in FUENTES_INGESTA.items()}
    assert evaluar(_metricas(ingesta=en_el_limite)) == []

    pasada = dict(en_el_limite, form4=FUENTES_INGESTA["form4"][1] + 1)
    (a,) = evaluar(_metricas(ingesta=pasada))
    assert a.codigo == "ingesta_form4_parada" and a.gravedad == "critica"
    assert "131 días" in a.mensaje and "Form 4" in a.mensaje


def test_el_form_4_tolera_el_retraso_trimestral_de_la_sec():
    """Los Form 4 de un trimestre llegan semanas después de cerrarlo: con el último
    dato de hace 100 días (caso normal a mitad de trimestre) no se avisa."""
    assert evaluar(_metricas(ingesta=dict(_al_dia(), form4=100))) == []


def test_una_fuente_sin_ninguna_fila_avisa():
    assert _codigos(_metricas(ingesta=dict(_al_dia(), **{"8k": None}))) == {"ingesta_8k_vacia"}


def test_varias_fuentes_paradas_dan_una_alerta_cada_una():
    m = _metricas(ingesta=dict(_al_dia(), mercado=30, **{"13dg": 90}))
    assert _codigos(m) == {"ingesta_mercado_parada", "ingesta_13dg_parada"}


def test_sin_contacto_para_la_sec_avisa_como_critica():
    (a,) = evaluar(_metricas(sec_contacto_ok=False))
    assert a.codigo == "sec_sin_contacto" and a.gravedad == "critica"


def test_un_paso_fallido_de_la_pasada_avisa_por_su_nombre():
    m = _metricas(pasos_fallidos=["Mercado (lote)", "Eventos 8-K"])
    assert _codigos(m) == {"paso_fallido_Mercado (lote)", "paso_fallido_Eventos 8-K"}


def test_las_metricas_viejas_sin_ingesta_siguen_valiendo():
    """Las reglas anteriores se probaban con diccionarios sin estas claves."""
    assert evaluar({"llamadas": {}, "calidad": {}}) == []


# ---------- la consulta ----------

class _CursorFalso:
    def __init__(self, valores):
        self.valores = list(valores)

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return (self.valores.pop(0),)


def test_leer_ingesta_devuelve_dias_enteros_y_none_si_la_tabla_esta_vacia():
    from decimal import Decimal
    # el orden de la consulta es el de FUENTES_INGESTA: form4, mercado, 8k, 13dg, 10q
    cur = _CursorFalso([35, Decimal("1"), Decimal("1.0"), None, 35])

    assert leer_ingesta(cur) == {"form4": 35, "mercado": 1, "8k": 1, "13dg": None, "10q": 35}


def test_el_form_4_se_mide_por_fecha_de_transaccion_no_por_fecha_de_carga():
    """fecha_registro no se toca al recargar (upsert): mediría la primera carga
    y avisaría de una ingesta que acaba de funcionar."""
    consultas = []

    class Cur(_CursorFalso):
        def execute(self, sql):
            consultas.append(sql)

    leer_ingesta(Cur([0, 0, 0, 0, 0]))

    form4 = next(c for c in consultas if "insider_transactions" in c)
    assert "fecha_transaccion" in form4 and "fecha_registro" not in form4


def test_el_contacto_de_la_sec_por_defecto_cuenta_como_sin_configurar(monkeypatch):
    for valor, esperado in [("", False), ("configura-SEC_CONTACT_EMAIL@en-tu-.env", False),
                            ("sin-arroba", False), ("yo@correo.com", True)]:
        monkeypatch.setenv("SEC_CONTACT_EMAIL", valor)
        assert alertas.sec_contacto_configurado() is esperado, valor
    monkeypatch.delenv("SEC_CONTACT_EMAIL")
    assert alertas.sec_contacto_configurado() is False


# ---------- el envío y el orquestador ----------

def test_revisar_cuenta_los_pasos_fallidos_en_el_mismo_mensaje(monkeypatch):
    enviados = []
    monkeypatch.setattr("notificador_telegram.enviar_telegram", lambda t: enviados.append(t) or True)
    monkeypatch.setattr(alertas, "leer_metricas", lambda conn: _metricas(coste_hoy_usd=5.0))

    r = revisar_y_avisar(object(), pasos_fallidos=["Eventos 8-K"])

    assert len(r) == 2 and len(enviados) == 1, "coste + paso fallido, un solo mensaje"
    assert "Eventos 8-K" in enviados[0]


def test_un_paso_no_critico_que_falla_se_anota_y_uno_que_va_bien_no(monkeypatch):
    class Proceso:
        def __init__(self, codigo):
            self.returncode = codigo

    codigos = iter([1, 0])
    monkeypatch.setattr(orquestador.subprocess, "run", lambda *a, **k: Proceso(next(codigos)))
    orquestador.PASOS_FALLIDOS.clear()

    orquestador.ejecutar_paso("Eventos 8-K", ["ingesta_8k.py"], critico=False)
    orquestador.ejecutar_paso("13D/G y shelf", ["ingesta_13dg.py"], critico=False)

    assert orquestador.PASOS_FALLIDOS == ["Eventos 8-K"]
    orquestador.PASOS_FALLIDOS.clear()


def test_un_paso_que_pasa_del_tiempo_maximo_tambien_se_anota(monkeypatch):
    import subprocess

    def cuelga(comando, timeout=None, **kw):
        raise subprocess.TimeoutExpired(comando, timeout)

    monkeypatch.setattr(orquestador.subprocess, "run", cuelga)
    orquestador.PASOS_FALLIDOS.clear()

    orquestador.ejecutar_paso("Mercado (lote)", ["mercado_yfinance.py"], critico=False)

    assert orquestador.PASOS_FALLIDOS == ["Mercado (lote)"]
    orquestador.PASOS_FALLIDOS.clear()


def test_la_pasada_empieza_sin_fallos_heredados_y_los_pasa_a_las_alertas(monkeypatch):
    recibidos = []

    class Conn:
        def cursor(self):
            return self

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return []

        def close(self):
            pass

    def paso(nombre, args, critico):
        if nombre == "Eventos 8-K":
            orquestador.PASOS_FALLIDOS.append(nombre)
        return True

    monkeypatch.setattr(orquestador, "conectar_db", lambda: Conn())
    monkeypatch.setattr(orquestador, "fase_backup", lambda: True)   # no copiar la base de verdad
    monkeypatch.setattr(orquestador, "fase_cambios", lambda: {})
    monkeypatch.setattr(orquestador, "ejecutar_paso", paso)
    monkeypatch.setattr(orquestador, "fase_agentes", lambda c: {"analizadas": 0, "fallos": 0})
    monkeypatch.setattr(orquestador, "fase_auditorias", lambda c: {"auditadas": 0})
    monkeypatch.setattr(orquestador, "fase_seguimiento", lambda: {})
    monkeypatch.setattr(orquestador, "fase_contexto_sector", lambda: {})
    monkeypatch.setattr(orquestador, "fase_alertas", lambda pasos=None: recibidos.append(pasos) or [])
    monkeypatch.setattr(orquestador, "enviar_telegram", lambda t: None)

    class Args:
        solo_agentes = False
        completo = False

    orquestador.PASOS_FALLIDOS[:] = ["resto de una pasada anterior"]
    orquestador.una_pasada(Args())

    assert recibidos == [["Eventos 8-K"]], "lo de la pasada anterior no se arrastra"
    orquestador.PASOS_FALLIDOS.clear()
