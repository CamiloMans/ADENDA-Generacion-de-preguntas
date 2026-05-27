from __future__ import annotations

from app.pipeline.extract import (
    build_output,
    detect_sections_and_observations,
    split_embedded_observation_blocks,
)


def _text_item(text: str, y: float) -> dict:
    return {
        "kind": "text",
        "page": 1,
        "bbox": (0.0, y, 100.0, y + 10.0),
        "text": text,
        "prefix": None,
    }


def test_split_embedded_observation_and_keep_nch_1333_inside_7_5() -> None:
    items = [
        _text_item(
            "7. Efectos, caracteristicas o circunstancias del Articulo 11",
            10.0,
        ),
        _text_item(
            "7.4. En relacion con Riesgo a la salud, se solicita ampliar. "
            "Articulo 6 del RSEIA, del MMA 7.5. Respecto de la Respuesta "
            "6.5.4 de la Adenda, el Titular establece que el efluente cumplira la NCh",
            20.0,
        ),
        _text_item(
            "1.333. Al respecto, se solicita al Titular presentar los antecedentes "
            "de monitoreo y seguimiento (punto de monitoreo, frecuencia, tipo de "
            "muestreo, parametros a medir). Flora y vegetacion",
            30.0,
        ),
        _text_item("7.6. Otra observacion posterior, se solicita aclarar.", 40.0),
    ]

    split_items = split_embedded_observation_blocks(items)
    detect_sections_and_observations(split_items)
    output = build_output(split_items)

    by_id = {obs["observation_id"]: obs for obs in output}

    assert "1.333." not in by_id
    assert by_id["7.4."]["text"] == "7.4. En relacion con Riesgo a la salud, se solicita ampliar. Articulo 6 del RSEIA, del MMA"
    assert by_id["7.5."]["text"] == (
        "7.5. Respecto de la Respuesta 6.5.4 de la Adenda, el Titular "
        "establece que el efluente cumplira la NCh 1.333. Al respecto, "
        "se solicita al Titular presentar los antecedentes de monitoreo y "
        "seguimiento (punto de monitoreo, frecuencia, tipo de muestreo, "
        "parametros a medir)."
    )


def test_false_pending_section_does_not_block_current_section_observation() -> None:
    items = [
        _text_item("7. Prediccion y evaluacion del impacto ambiental", 10.0),
        _text_item("7.6.4. Se solicita aclarar antecedente previo.", 20.0),
        _text_item("21. Skytanthus acutus listado dentro de la observacion.", 30.0),
        _text_item("7.6.5. Respecto de la respuesta anterior, se solicita complementar.", 40.0),
    ]

    detect_sections_and_observations(items)
    output = build_output(items)

    by_id = {obs["observation_id"]: obs for obs in output}

    assert items[2]["observation_id"] == "7.6.4."
    assert items[3]["observation_id"] == "7.6.5."
    assert "7.6.5." in by_id


def test_sagasca_style_ids_without_final_dot_are_individual_questions() -> None:
    items = [
        _text_item("1 Descripcion de proyecto", 10.0),
        _text_item(
            "1.1 Respecto del volumen de extraccion anual, se debera precisar la cantidad.",
            20.0,
        ),
        _text_item(
            "1.5 Respecto del apartado 1.4. Localizacion del Proyecto, se tienen las siguientes observaciones:",
            30.0,
        ),
        _text_item(
            "1.5.1 En la figura 1-3 Partes y obras del proyecto, se debera incorporar el portal.",
            40.0,
        ),
        _text_item(
            "1.5.2 Debera complementar la figura 1-10 Concesiones mineras.",
            50.0,
        ),
        _text_item(
            "1.6 Respecto del apartado 1.5. Descripcion de las partes, obras y acciones del proyecto, se tienen las siguientes observaciones:",
            60.0,
        ),
        _text_item(
            "1.6.1 Debera aclarar si el proyecto contempla operaciones durante horario nocturno.",
            70.0,
        ),
    ]

    detect_sections_and_observations(items)
    output = build_output(items)
    by_id = {obs["observation_id"]: obs for obs in output}

    assert "1.5." not in by_id
    assert "1.1." in by_id
    assert "1.5.1." in by_id
    assert "1.5.2." in by_id
    assert "1.6.1." in by_id
    assert by_id["1.5.1."]["section_1"] == "1. Descripcion de proyecto"
    assert by_id["1.5.1."]["section_2"].startswith("1.5. Respecto del apartado")
    assert by_id["1.6.1."]["section_2"].startswith("1.6. Respecto del apartado")
