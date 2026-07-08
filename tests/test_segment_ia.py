from __future__ import annotations

import fitz

from app.pipeline.segment_ia import ensamblar


def _linea(lid: str, page: int, text: str) -> dict:
    return {"id": lid, "page": page, "bbox": [10, 10, 100, 20], "text": text}


def test_ensamblar_promueve_seccion2_huerfana(tmp_path) -> None:
    pdf = tmp_path / "doc.pdf"
    d = fitz.open()
    d.new_page()
    d.save(str(pdf))
    d.close()
    doc = fitz.open(str(pdf))

    texto_31 = (
        "3.1 En relacion con los antecedentes presentados en la Adenda y "
        "considerando que no se intervendran sitios arqueologicos ni "
        "paleontologicos durante las obras, se senala que el permiso no "
        "resulta aplicable al proyecto."
    )
    lineas = [
        _linea("L00000", 1, "3 Permisos y pronunciamientos"),
        _linea("L00001", 1, texto_31),
        _linea("L00002", 1, "3.2 Respecto de los antecedentes presentados para el "
                            "otorgamiento del permiso del articulo 140 del RSEIA, se senala:"),
        _linea("L00003", 1, "3.2.1 Se debera complementar la informacion relativa a las zonas de acopio."),
        _linea("L00004", 1, "Partes y obras"),
    ]
    segmentos = [
        {"tipo": "seccion_1", "id": "3.", "desde": "L00000", "hasta": "L00000"},
        {"tipo": "seccion_2", "id": "3.1", "desde": "L00001", "hasta": "L00001"},
        {"tipo": "seccion_2", "id": "3.2", "desde": "L00002", "hasta": "L00002"},
        {"tipo": "observacion", "id": "3.2.1", "desde": "L00003", "hasta": "L00003"},
        {"tipo": "seccion_2", "id": None, "desde": "L00004", "hasta": "L00004"},
    ]

    salida, _ = ensamblar(doc, str(pdf), lineas, segmentos, str(tmp_path), "doc")
    doc.close()

    ids = [o["observation_id"] for o in salida]
    # Subseccion huerfana con texto sustantivo: promovida a observacion propia.
    assert "3.1" in ids
    assert "3.2.1" in ids
    # Bisagra con hijas: NO se promueve; queda como section_2 de sus hijas.
    assert "3.2" not in ids
    obs321 = next(o for o in salida if o["observation_id"] == "3.2.1")
    assert (obs321["section_2"] or "").startswith("3.2 Respecto")
    obs31 = next(o for o in salida if o["observation_id"] == "3.1")
    assert obs31["text"].startswith("3.1 En relacion")
    assert obs31["section_2"] is None
    # Titulo corto huerfano (< umbral): no genera observacion.
    assert not any(oid.startswith("SEC-") for oid in ids)
    # Orden documental: 3.1 antes que 3.2.1.
    assert ids.index("3.1") < ids.index("3.2.1")
