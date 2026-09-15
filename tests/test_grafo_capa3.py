"""
tests/test_grafo_capa3.py — el grafo de LangGraph de la Capa 3, con
Detective y Auditor completamente simulados (nunca llama a Groq,
Gemini, ni a una base de datos real). Lo que pruebo es la ORQUESTACIÓN:
que el fan-out a los dos Detectives y el fan-in al Auditor se comportan
como digo en el docstring de grafo_capa3.py, no la lógica de análisis
en sí (esa ya la cubre tests/test_pipeline_offline.py).
"""

import pytest

import detective
import auditor
import grafo_capa3


@pytest.fixture(autouse=True)
def modelo_secundario_fijo(monkeypatch):
    # Sin esto, modelo_secundario() lee MODELO_SECUNDARIO del entorno —
    # lo fijo a "gemini" para que el grafo sea el mismo en cualquier
    # máquina que corra estos tests.
    monkeypatch.delenv("MODELO_SECUNDARIO", raising=False)


def _resultado_detective_falso(empresa_id=1):
    return {
        "contexto": {"empresa_id": empresa_id, "ticker": "FAKE"},
        "respuesta_llm": {"veredicto_preliminar": "INTERESANTE"},
        "verificacion_citas": {"alucinacion_detectada": False},
    }


def test_los_dos_detectives_corren_y_el_auditor_los_espera(monkeypatch):
    llamadas = {"detective": [], "guardar_detective": [], "auditor": 0, "guardar_auditor": 0}

    def fake_ejecutar_detective(conn, ticker, modelo):
        llamadas["detective"].append(modelo)
        return _resultado_detective_falso()

    def fake_guardar_resultado(conn, empresa_id, modelo, resultado):
        llamadas["guardar_detective"].append(modelo)

    def fake_ejecutar_auditor(conn, ticker, modelo_detective=None):
        llamadas["auditor"] += 1
        return [{"empresa_id": 1, "ticker": ticker, "veredicto_final": "INTERESANTE"}]

    def fake_guardar_resultado_auditor(conn, resultado):
        llamadas["guardar_auditor"] += 1

    monkeypatch.setattr(detective, "ejecutar_detective", fake_ejecutar_detective)
    monkeypatch.setattr(detective, "guardar_resultado", fake_guardar_resultado)
    monkeypatch.setattr(auditor, "ejecutar_auditor", fake_ejecutar_auditor)
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", fake_guardar_resultado_auditor)

    estado_final = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert sorted(llamadas["detective"]) == ["gemini", "groq"], \
        "los dos modelos por defecto (groq + secundario) deben correr"
    assert sorted(llamadas["guardar_detective"]) == ["gemini", "groq"]
    assert llamadas["auditor"] == 1, "el auditor corre una vez, tras esperar a los dos Detectives"
    assert llamadas["guardar_auditor"] == 1
    assert estado_final["resultado_groq"] is not None
    assert estado_final["resultado_secundario"] is not None
    assert len(estado_final["auditorias_guardadas"]) == 1


def test_un_detective_falla_pero_el_auditor_sigue_corriendo(monkeypatch):
    def fake_ejecutar_detective(conn, ticker, modelo):
        if modelo == "groq":
            return None  # simula "no encontrado" o texto_mda ausente
        return _resultado_detective_falso()

    auditor_llamado = {"veces": 0}

    def fake_ejecutar_auditor(conn, ticker, modelo_detective=None):
        auditor_llamado["veces"] += 1
        return []

    monkeypatch.setattr(detective, "ejecutar_detective", fake_ejecutar_detective)
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: None)
    monkeypatch.setattr(auditor, "ejecutar_auditor", fake_ejecutar_auditor)
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", lambda *a, **k: None)

    estado_final = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert estado_final["fallo_groq"] is True
    assert estado_final["resultado_secundario"] is not None
    assert auditor_llamado["veces"] == 1, "con UN detective vivo, el auditor sigue teniendo sentido"


def test_los_dos_detectives_fallan_el_auditor_no_corre(monkeypatch):
    monkeypatch.setattr(detective, "ejecutar_detective", lambda conn, ticker, modelo: None)
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: None)

    auditor_llamado = {"veces": 0}
    monkeypatch.setattr(
        auditor, "ejecutar_auditor",
        lambda *a, **k: auditor_llamado.__setitem__("veces", auditor_llamado["veces"] + 1) or [],
    )

    estado_final = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert estado_final["fallo_groq"] is True
    assert estado_final["fallo_secundario"] is True
    assert auditor_llamado["veces"] == 0, "sin ningún Detective vivo, no hay nada que auditar"
    assert estado_final["auditorias_guardadas"] == []


def test_modelos_a_ejecutar_restringe_que_detective_corre(monkeypatch):
    llamadas = []
    monkeypatch.setattr(
        detective, "ejecutar_detective",
        lambda conn, ticker, modelo: (llamadas.append(modelo), _resultado_detective_falso())[1],
    )
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: None)
    monkeypatch.setattr(auditor, "ejecutar_auditor", lambda *a, **k: [])
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", lambda *a, **k: None)

    estado_final = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE", modelos_a_ejecutar=["groq"])

    assert llamadas == ["groq"], "solo debe correr el modelo pedido, no el secundario"
    assert estado_final["resultado_groq"] is not None
    assert estado_final["resultado_secundario"] is None
    assert estado_final["fallo_secundario"] is False, \
        "el modelo que no se pidió no cuenta como fallo, simplemente no corrió"


def test_excepcion_en_detective_se_convierte_en_fallo_no_en_crash(monkeypatch):
    def _explota(conn, ticker, modelo):
        raise RuntimeError("Groq devolvió 429")

    monkeypatch.setattr(detective, "ejecutar_detective", _explota)
    monkeypatch.setattr(detective, "guardar_resultado", lambda *a, **k: None)
    monkeypatch.setattr(auditor, "ejecutar_auditor", lambda *a, **k: [])
    monkeypatch.setattr(auditor, "guardar_resultado_auditor", lambda *a, **k: None)

    # No debe propagar la excepción -- el grafo la convierte en fallo_*
    estado_final = grafo_capa3.ejecutar_grafo_capa3(conn=object(), ticker="FAKE")

    assert estado_final["fallo_groq"] is True
    assert estado_final["fallo_secundario"] is True
