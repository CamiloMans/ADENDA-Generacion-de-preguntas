from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.core.text_normalization import repair_mojibake_data, repair_mojibake_text
from app.pipeline.classify import TAXONOMIA
from app.services.result_service import _build_question


def test_repair_mojibake_text_fixes_common_utf8_latin1_mixups() -> None:
    assert repair_mojibake_text("Flora y VegetaciÃ³n") == "Flora y Vegetación"
    assert repair_mojibake_text("Recurso HÃ\xaddrico") == "Recurso Hídrico"
    assert repair_mojibake_text("Área de Influencia") == "Área de Influencia"


def test_repair_mojibake_data_repairs_nested_payloads() -> None:
    payload = {
        "tema": "Calidad del Aire y Emisiones AtmosfÃ©ricas",
        "temas": [
            {"nombre": "Flora y VegetaciÃ³n"},
            {"nombre": "Recurso HÃ\xaddrico"},
        ],
    }

    repaired = repair_mojibake_data(payload)

    assert repaired["tema"] == "Calidad del Aire y Emisiones Atmosféricas"
    assert repaired["temas"][0]["nombre"] == "Flora y Vegetación"
    assert repaired["temas"][1]["nombre"] == "Recurso Hídrico"


def test_taxonomia_is_normalized_on_import() -> None:
    assert TAXONOMIA["FLORA_VEGETACION"]["nombre"] == "Flora y Vegetación"
    assert TAXONOMIA["CALIDAD_AIRE"]["nombre"] == "Calidad del Aire y Emisiones Atmosféricas"
    assert TAXONOMIA["RECURSO_HIDRICO"]["nombre"] == "Recurso Hídrico, Hidrología e Hidrogeología"


def test_build_question_normalizes_strings_before_persisting() -> None:
    job = SimpleNamespace(id=uuid4(), adenda_id=35)
    item = {
        "observation_id": "5.1.",
        "section_1": "Medio Humano",
        "section_2": "VegetaciÃ³n y uso de suelo",
        "requirement_types": ["aclaraciÃ³n"],
        "clasificacion": {
            "tema_principal": "Flora y VegetaciÃ³n",
            "tema_principal_id": "FLORA_VEGETACION",
            "score": 4,
            "temas_principales": [
                {"id": "FLORA_VEGETACION", "nombre": "Flora y VegetaciÃ³n", "score": 4}
            ],
            "temas_secundarios": [
                {"id": "HIDRICO", "nombre": "Recurso HÃ\xaddrico", "score": 2}
            ],
            "keywords_match": ["vegetaciÃ³n", "hÃ¡bitat"],
        },
        "text": "Se solicita complementar la lÃ­nea base.",
        "tables": [],
        "images": [],
    }

    question = _build_question(job=job, item=item, order=1, used_numbers=set())

    assert question.bisagra == "Vegetación y uso de suelo"
    assert question.requirement_types == ["aclaración"]
    assert question.tema_principal == "Flora y Vegetación"
    assert question.temas_principales == ["Flora y Vegetación"]
    assert question.temas_secundarios == [{"id": "HIDRICO", "nombre": "Recurso Hídrico", "score": 2}]
    assert question.keywords_match == ["vegetación", "hábitat"]
    assert question.texto == "Se solicita complementar la línea base."
    assert question.raw_question_json["clasificacion"]["tema_principal"] == "Flora y Vegetación"
