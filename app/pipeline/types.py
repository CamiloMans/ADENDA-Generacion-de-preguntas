from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class ExtractionSummary:
    pages: int
    observaciones: int
    tablas: int
    imagenes: int
    output_dir: Path
    output_json: Path
    tables_json: Path
    summary_json: Path
    images_dir: Path
    tables_dir: Path

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["output_json"] = str(self.output_json)
        data["tables_json"] = str(self.tables_json)
        data["summary_json"] = str(self.summary_json)
        data["images_dir"] = str(self.images_dir)
        data["tables_dir"] = str(self.tables_dir)
        return data


@dataclass(slots=True)
class ClassificationSummary:
    total: int
    classified: int
    unclassified: int
    output_json: Path
    output_detail_json: Path

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_json"] = str(self.output_json)
        data["output_detail_json"] = str(self.output_detail_json)
        return data


@dataclass(slots=True)
class ReviewSummary:
    total_observaciones: int
    ids_faltantes_detectados: int
    ids_extraidas_desde_pdf: int
    ids_no_localizadas: int
    correcciones_revision: int
    output_json: Path
    audit_json: Path
    report_md: Path

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_json"] = str(self.output_json)
        data["audit_json"] = str(self.audit_json)
        data["report_md"] = str(self.report_md)
        return data
