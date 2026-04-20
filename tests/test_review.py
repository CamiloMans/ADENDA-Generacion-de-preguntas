from __future__ import annotations

from app.pipeline.review import aplicar_correcciones
from app.services.google_drive_service import DriveFileRef
from app.services.result_service import _build_question_media_rows


def test_aplicar_correcciones_preserves_table_file_metadata() -> None:
    observacion = {
        "observation_id": "1.1.",
        "text": "Texto original",
        "tables": [
            {
                "table_file": "C:/tmp/tabla_001.png",
                "caption": "Tabla original",
                "rows": [["antes"]],
                "page": 7,
            }
        ],
        "images": [
            {
                "image_file": "C:/tmp/image_001.png",
                "caption": "Imagen original",
            }
        ],
    }
    resultado = {
        "c2_texto_corregido": None,
        "c2_problema": None,
        "c3_problema": "OCR split rows",
        "c3_tablas_corregidas": [
            {
                "caption": "Tabla corregida",
                "rows": [["despues"]],
            }
        ],
    }

    corregida, auditoria = aplicar_correcciones(observacion, resultado)

    assert corregida["tables"][0]["table_file"] == "C:/tmp/tabla_001.png"
    assert corregida["tables"][0]["caption"] == "Tabla corregida"
    assert corregida["tables"][0]["rows"] == [["despues"]]
    assert corregida["tables"][0]["page"] == 7
    assert corregida["images"][0]["image_file"] == "C:/tmp/image_001.png"
    assert auditoria["c3_tablas_anteriores"] == observacion["tables"]


def test_aplicar_correcciones_replaces_symbolic_table_ref_with_original_file() -> None:
    observacion = {
        "observation_id": "12.6.",
        "text": "Texto original",
        "tables": [
            {
                "table_file": "C:/tmp/page_045_table_001.png",
                "rows": [["parte 1"]],
            }
        ],
        "images": [],
    }
    resultado = {
        "c2_texto_corregido": None,
        "c2_problema": None,
        "c3_problema": "Merged split table",
        "c3_tablas_corregidas": [
            {
                "table_file": "merged_table",
                "rows": [["fusionada"]],
            }
        ],
    }

    corregida, _ = aplicar_correcciones(observacion, resultado)

    assert corregida["tables"][0]["table_file"] == "C:/tmp/page_045_table_001.png"
    assert corregida["tables"][0]["rows"] == [["fusionada"]]


def test_build_question_media_rows_skips_missing_media_refs() -> None:
    media_lookup = {
        "https://drive.example/media/tabla_001.png": DriveFileRef(
            file_id="table-1",
            filename="tabla_001.png",
            mime_type="image/png",
            web_view_url="https://drive.example/media/tabla_001.png",
            preview_url="https://drive.example/media/tabla_001.png/preview",
            download_url="https://drive.example/media/tabla_001.png/download",
            size_bytes=10,
            sha256="sha-table",
        ),
        "https://drive.example/media/image_001.png": DriveFileRef(
            file_id="image-1",
            filename="image_001.png",
            mime_type="image/png",
            web_view_url="https://drive.example/media/image_001.png",
            preview_url="https://drive.example/media/image_001.png/preview",
            download_url="https://drive.example/media/image_001.png/download",
            size_bytes=10,
            sha256="sha-image",
        ),
    }
    item = {
        "tables": [
            {"rows": [["sin ref"]]},
            {
                "table_file": "https://drive.example/media/tabla_001.png",
                "rows": [["ok"]],
            },
        ],
        "images": [
            {"caption": "sin ref"},
            {
                "image_file": "https://drive.example/media/image_001.png",
                "caption": "ok",
            },
        ],
    }

    rows = _build_question_media_rows(question_id=123, item=item, media_lookup=media_lookup)

    assert len(rows) == 2
    assert {row.tipo for row in rows} == {"tabla", "figura"}
    assert {row.filename for row in rows} == {"tabla_001.png", "image_001.png"}


def test_build_question_media_rows_skips_unresolvable_media_refs() -> None:
    media_lookup = {
        "https://drive.example/media/tabla_001.png": DriveFileRef(
            file_id="table-1",
            filename="tabla_001.png",
            mime_type="image/png",
            web_view_url="https://drive.example/media/tabla_001.png",
            preview_url="https://drive.example/media/tabla_001.png/preview",
            download_url="https://drive.example/media/tabla_001.png/download",
            size_bytes=10,
            sha256="sha-table",
        )
    }
    item = {
        "tables": [
            {"table_file": "merged_table", "rows": [["sin archivo real"]]},
            {
                "table_file": "https://drive.example/media/tabla_001.png",
                "rows": [["ok"]],
            },
        ],
        "images": [],
    }

    rows = _build_question_media_rows(question_id=123, item=item, media_lookup=media_lookup)

    assert len(rows) == 1
    assert rows[0].tipo == "tabla"
    assert rows[0].filename == "tabla_001.png"
