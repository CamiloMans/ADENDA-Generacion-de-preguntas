from __future__ import annotations

from app.pipeline.classify import clasificar_observacion


def test_nch_1333_effluent_question_classifies_as_water_resource() -> None:
    obs = {
        "section_1": "7. Efectos, caracteristicas o circunstancias del Articulo 11",
        "section_2": None,
        "text": (
            "7.5. Respecto de la Respuesta 6.5.4 de la Adenda, el Titular "
            "establece que el efluente cumplira la NCh 1.333. Al respecto, "
            "se solicita al Titular presentar los antecedentes de monitoreo "
            "y seguimiento (punto de monitoreo, frecuencia, tipo de muestreo, "
            "parametros a medir)."
        ),
    }

    result = clasificar_observacion(obs)

    assert result["tema_principal_id"] == "RECURSO_HIDRICO"
