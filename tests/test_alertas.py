"""
tests/test_alertas.py — las reglas de alerta, con diccionarios de métricas en
vez de base de datos (por eso leer_metricas y evaluar están separadas), y el
envío por Telegram, simulado.
"""

import alertas
from alertas import Alerta, evaluar, formatear, revisar_y_avisar


def _metricas(**kw):
    base = {
        "llamadas": {}, "gemini_usadas_hoy": 0, "gemini_429_hoy": 0,
        "coste_hoy_usd": 0.0, "calidad": {},
    }
    base.update(kw)
    return base


def _codigos(m):
    return {a.codigo for a in evaluar(m)}


def test_todo_en_orden_no_alerta():
    m = _metricas(
        llamadas={"claude": {"total": 20, "fallos": 2, "por_categoria": {"sobrecarga_503": 2}}},
        gemini_usadas_hoy=10, coste_hoy_usd=0.4,
        calidad={"claude": {"analisis": 10, "pct_soportado": 95.0, "alucinaciones": 0}},
    )
    assert evaluar(m) == []


def test_tasa_de_error_alta_alerta_y_dice_la_causa_principal():
    m = _metricas(llamadas={"claude": {
        "total": 10, "fallos": 6, "por_categoria": {"json_invalido": 4, "sobrecarga_503": 2},
    }})
    (a,) = evaluar(m)
    assert a.codigo == "errores_claude" and a.gravedad == "critica"
    assert "6 de 10" in a.mensaje and "json_invalido (4)" in a.mensaje


def test_pocas_llamadas_no_bastan_para_juzgar_la_tasa():
    m = _metricas(llamadas={"claude": {"total": 3, "fallos": 3, "por_categoria": {"json_invalido": 3}}})
    assert "errores_claude" not in _codigos(m)


def test_la_cuota_agotada_no_cuenta_como_tasa_de_error():
    """Gemini llega a su límite todos los días: eso tiene su propia alerta, no
    debe disparar también la de errores del modelo."""
    m = _metricas(
        llamadas={"gemini": {"total": 30, "fallos": 12, "por_categoria": {"cuota_429": 12}}},
        gemini_usadas_hoy=20, gemini_429_hoy=12,
    )
    assert _codigos(m) == {"cuota_gemini_agotada"}


def test_truncado_alerta_aunque_la_tasa_sea_baja():
    m = _metricas(llamadas={"claude": {"total": 50, "fallos": 2, "por_categoria": {"truncado": 2}}})
    (a,) = evaluar(m)
    assert a.codigo == "truncado_claude" and "max_tokens" in a.mensaje


def test_cuota_gemini_casi_agotada_y_agotada():
    assert _codigos(_metricas(gemini_usadas_hoy=17)) == set()
    assert _codigos(_metricas(gemini_usadas_hoy=18)) == {"cuota_gemini_casi"}
    assert _codigos(_metricas(gemini_usadas_hoy=20, gemini_429_hoy=1)) == {"cuota_gemini_agotada"}, \
        "agotada sustituye a casi agotada, no se avisa dos veces"


def test_gasto_diario_sobre_el_tope():
    assert _codigos(_metricas(coste_hoy_usd=alertas.UMBRAL_COSTE_DIARIO_USD)) == set()
    assert _codigos(_metricas(coste_hoy_usd=alertas.UMBRAL_COSTE_DIARIO_USD + 0.01)) == {"coste_diario"}


def test_calidad_de_citas_y_alucinaciones():
    m = _metricas(calidad={
        "claude": {"analisis": 6, "pct_soportado": 62.0, "alucinaciones": 2},
        "gemini": {"analisis": 2, "pct_soportado": 10.0, "alucinaciones": 2},  # pocos análisis: no se juzga
    })
    assert _codigos(m) == {"calidad_claude", "alucinacion_claude"}


def test_el_mensaje_pone_las_criticas_primero_y_cuenta_las_alertas():
    texto = formatear([
        Alerta("coste_diario", "aviso", "Gasto alto"),
        Alerta("errores_claude", "critica", "Claude falla"),
    ])
    assert texto.splitlines()[0].endswith("2 alerta(s)")
    assert texto.index("Claude falla") < texto.index("Gasto alto")


class _ConexionFalsa:
    pass


def test_revisar_manda_un_solo_mensaje_y_solo_si_hay_alertas(monkeypatch):
    enviados = []
    monkeypatch.setattr("notificador_telegram.enviar_telegram", lambda t: enviados.append(t) or True)

    monkeypatch.setattr(alertas, "leer_metricas", lambda conn: _metricas())
    assert revisar_y_avisar(_ConexionFalsa()) == [] and enviados == [], "sin alertas no hay mensaje"

    monkeypatch.setattr(alertas, "leer_metricas", lambda conn: _metricas(coste_hoy_usd=5.0, gemini_usadas_hoy=19))
    assert len(revisar_y_avisar(_ConexionFalsa())) == 2
    assert len(enviados) == 1, "varias alertas, un solo mensaje"

    assert len(revisar_y_avisar(_ConexionFalsa(), enviar=False)) == 2 and len(enviados) == 1


def test_una_alerta_que_falla_no_rompe_la_pasada(monkeypatch):
    def explota(conn):
        raise RuntimeError("la base no responde")

    monkeypatch.setattr(alertas, "leer_metricas", explota)
    assert revisar_y_avisar(_ConexionFalsa()) == []


def test_la_fase_de_alertas_del_orquestador_nunca_lanza(monkeypatch):
    import orquestador

    def sin_base():
        raise ConnectionError("sin base de datos")

    monkeypatch.setattr(orquestador, "conectar_db", sin_base)
    assert orquestador.fase_alertas() == []


def test_el_mensaje_a_telegram_va_con_el_html_escapado(monkeypatch):
    """enviar_telegram usa parse_mode=HTML: un '<' o un '&' sin escapar haría
    que Telegram rechace el mensaje entero."""
    enviados = []
    monkeypatch.setattr("notificador_telegram.enviar_telegram", lambda t: enviados.append(t) or True)
    monkeypatch.setattr(alertas, "leer_metricas", lambda conn: _metricas(
        llamadas={"claude": {"total": 10, "fallos": 8, "por_categoria": {"error_proveedor": 8}}}))
    monkeypatch.setattr(alertas, "evaluar", lambda m: [Alerta("x", "critica", "falla <b> & más")])

    revisar_y_avisar(_ConexionFalsa())

    assert "&lt;b&gt; &amp; más" in enviados[0] and "<b>" not in enviados[0]
