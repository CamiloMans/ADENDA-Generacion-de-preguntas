from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class ExtractionSummary:
    pages: int
    capitulos: int
    bisagras: int
    preguntas: int
    tablas: int
    figuras: int
    total_detections: int
    output_dir: Path

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
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
