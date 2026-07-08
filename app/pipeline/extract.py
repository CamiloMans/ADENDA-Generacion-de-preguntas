import os
import re
import json
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Optional, Tuple

import fitz  # PyMuPDF

from app.pipeline.types import ExtractionSummary

# =========================================================
# NUEVO: pdfplumber como motor principal de tablas
# =========================================================
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False


PDF_PATH = os.getenv("ICSARA_PDF_PATH", "")
BASE_DIR = os.getenv("ICSARA_BASE_DIR", str(Path("salida_icsara").resolve()))


# =========================================================
# OCR OPCIONAL PARA TABLAS DESDE IMAGEN
# =========================================================
OCR_TABLES_FROM_IMAGES = True

try:
    from PIL import Image
    import pytesseract
    TESSERACT_AVAILABLE = True
except Exception:
    TESSERACT_AVAILABLE = False


# =========================================================
# UTILIDADES GENERALES
# =========================================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def norm(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def one_line(text: str) -> str:
    text = norm(text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def pdf_stem(pdf_path: str) -> str:
    return os.path.splitext(os.path.basename(pdf_path))[0]


def bbox_union(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    return (
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    )


def rect_area(rect: fitz.Rect) -> float:
    return max(0.0, float(rect.width)) * max(0.0, float(rect.height))


def tuple_to_rect(bbox: Tuple[float, float, float, float]) -> fitz.Rect:
    return fitz.Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))


def rect_to_tuple(rect: fitz.Rect) -> Tuple[float, float, float, float]:
    return (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))


def rect_to_dict(rect: fitz.Rect) -> Dict[str, float]:
    return {
        "x0": round(float(rect.x0), 3),
        "y0": round(float(rect.y0), 3),
        "x1": round(float(rect.x1), 3),
        "y1": round(float(rect.y1), 3),
        "width": round(float(rect.width), 3),
        "height": round(float(rect.height), 3),
        "area": round(float(rect_area(rect)), 3),
    }


def union_rect(a: fitz.Rect, b: fitz.Rect) -> fitz.Rect:
    return fitz.Rect(
        min(a.x0, b.x0),
        min(a.y0, b.y0),
        max(a.x1, b.x1),
        max(a.y1, b.y1),
    )


def intersection_area_rect(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = a & b
    if inter.is_empty:
        return 0.0
    return rect_area(inter)


def iou_rect(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = intersection_area_rect(a, b)
    if inter <= 0:
        return 0.0
    union = rect_area(a) + rect_area(b) - inter
    return inter / union if union > 0 else 0.0


def expand_rect(rect: fitz.Rect, pad: float, page_rect: fitz.Rect) -> fitz.Rect:
    rr = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    return rr & page_rect


def rects_touch_or_overlap(a: fitz.Rect, b: fitz.Rect, tol: float = 0.0) -> bool:
    aa = fitz.Rect(a.x0 - tol, a.y0 - tol, a.x1 + tol, a.y1 + tol)
    bb = fitz.Rect(b.x0 - tol, b.y0 - tol, b.x1 + tol, b.y1 + tol)
    return not (aa & bb).is_empty


def merge_rects(rects: List[fitz.Rect], gap: float = 5.0) -> List[fitz.Rect]:
    if not rects:
        return []

    pending = rects[:]
    merged: List[fitz.Rect] = []

    while pending:
        current = pending.pop(0)
        changed = True

        while changed:
            changed = False
            keep = []
            for r in pending:
                if rects_touch_or_overlap(current, r, tol=gap):
                    current = union_rect(current, r)
                    changed = True
                else:
                    keep.append(r)
            pending = keep

        merged.append(current)

    return merged


def deduplicate_rects(rects: List[fitz.Rect], iou_thr: float = 0.65) -> List[fitz.Rect]:
    out: List[fitz.Rect] = []
    for r in sorted(rects, key=lambda z: rect_area(z), reverse=True):
        if not any(iou_rect(r, k) >= iou_thr for k in out):
            out.append(r)
    return sorted(out, key=lambda z: (z.y0, z.x0))


def is_inside_header_footer(
    rect: fitz.Rect,
    page_rect: fitz.Rect,
    header_ratio: float = 0.08,
    footer_ratio: float = 0.08,
) -> bool:
    header_limit = page_rect.y0 + page_rect.height * header_ratio
    footer_limit = page_rect.y1 - page_rect.height * footer_ratio
    cy = (rect.y0 + rect.y1) / 2.0
    return cy <= header_limit or cy >= footer_limit


def is_footer_or_noise(text: str) -> bool:
    t = one_line(text)
    if not t:
        return True
    if "Para validar las firmas de este documento" in t:
        return True
    if "https://validador.sea.gob.cl/validar/" in t:
        return True
    if re.fullmatch(r"\d{2} de [A-Za-zÃ¡Ã©Ã­Ã³ÃºÃ±ÃÃ‰ÃÃ“ÃšÃ‘]+ de \d{4}\.?", t):
        return True
    return False


def is_footer_zone(
    bbox: Tuple[float, float, float, float],
    page_height: float,
    threshold: float = 0.9,
) -> bool:
    y0 = bbox[1]
    limit = page_height * threshold
    return y0 >= limit


# =========================================================
# PATRONES ICSARA
# =========================================================

RE_OBS = re.compile(r"^\s*((?:\d+\.){2,}\d+\.?)\s+(.+?)\s*$")
RE_SEC2 = re.compile(r"^\s*(\d+\.\d+\.?)\s+(.+?)\s*$")
RE_SEC1 = re.compile(r"^\s*(\d+\.?)\s+(.+?)\s*$")
RE_FIG = re.compile(r"^\s*Figura\s+N[Â°Âº]?\s*\d+", re.IGNORECASE)
RE_TABLA = re.compile(r"^\s*Tabla(?:\s+N[Â°Âº]?\s*\d+)?", re.IGNORECASE)
RE_TABLA_STRICT = re.compile(r"^\s*Tabla\s+\S", re.IGNORECASE)
RE_PREFIX = re.compile(r"^\s*((?:\d+\.)+\d+\.?|\d+\.?)(?=\s)")
RE_SECTION_NUM = re.compile(r"^\s*(\d+)(?:\.|\s|$)")
RE_TRAILING_TOPIC_HEADING = re.compile(
    r'([.:)\]"])\s+('
    r"Flora\s+y\s+vegetaci[oó]n|"
    r"Flora\s+y\s+vegetacion"
    r")\s*$",
    re.IGNORECASE,
)
RE_EMBEDDED_SEC2_OBS = re.compile(
    r"\s+(?P<prefix>\d+\.\d+\.?)\s+"
    r"(?P<body>"
    r"Respecto(?:\s+de|\s+del|\s+a)?|"
    r"Con\s+respecto(?:\s+a)?|"
    r"En\s+relaci[oó]n(?:\s+con|\s+a)?|"
    r"Al\s+respecto|"
    r"Se\s+solicita|"
    r"El\s+Titular|"
    r"La\s+Titular"
    r")\b",
    re.IGNORECASE,
)


def get_prefix(text: str) -> Optional[str]:
    m = RE_PREFIX.match(text or "")
    return m.group(1) if m else None


def canonical_numbered_id(prefix: str) -> str:
    prefix = (prefix or "").strip()
    return prefix if prefix.endswith(".") else f"{prefix}."


def canonical_section_label(prefix: str, name: str) -> str:
    return f"{canonical_numbered_id(prefix)} {name}".strip()


def is_observation_prefix(prefix: Optional[str]) -> bool:
    if not prefix:
        return False
    parts = [part for part in prefix.strip().split(".") if part]
    return len(parts) >= 3


def section_number_from_text(text: Optional[str]) -> Optional[int]:
    if not text:
        return None

    m = RE_SECTION_NUM.match(text)
    if not m:
        return None

    try:
        return int(m.group(1))
    except ValueError:
        return None


def prefix_first_number(prefix: Optional[str]) -> Optional[int]:
    if not prefix:
        return None

    first = prefix.strip().split(".", 1)[0]
    try:
        return int(first)
    except ValueError:
        return None


def prefix_depth(prefix: Optional[str]) -> int:
    if not prefix:
        return 0
    return len([part for part in prefix.strip().split(".") if part])


def prefix_matches_section(
    prefix: str,
    current_sec1_name: Optional[str],
    pending_sec1: Optional[Tuple[str, str]] = None,
) -> bool:
    """
    Observation IDs must belong to the active top-level section.

    This prevents legal/normative references such as "NCh 1.333.",
    "Ley 19.300." or dates from becoming observations when they appear
    inside another section.
    """
    prefix_num = prefix_first_number(prefix)
    if prefix_num is None:
        return True

    pending_num = section_number_from_text(pending_sec1[0]) if pending_sec1 else None
    if pending_num is not None and prefix_num == pending_num:
        return True

    current_num = section_number_from_text(current_sec1_name)
    if current_num is None:
        return True

    return prefix_num == current_num


def _split_text_on_embedded_observations(
    text: str,
    current_section_num: Optional[int],
) -> List[str]:
    if not text or current_section_num is None:
        return [text]

    matches = [
        m
        for m in RE_EMBEDDED_SEC2_OBS.finditer(text)
        if prefix_first_number(m.group("prefix")) == current_section_num
    ]
    if not matches:
        return [text]

    parts: List[str] = []
    start = 0
    for match in matches:
        split_at = match.start("prefix")
        before = text[start:split_at].strip()
        if before:
            parts.append(before)
        start = split_at

    tail = text[start:].strip()
    if tail:
        parts.append(tail)

    return parts or [text]


def split_embedded_observation_blocks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Split text blocks where PDF extraction merged two observations.

    Example:
    "... Articulo 6 ... 7.5. Respecto de la Respuesta ..."

    The split only fires for IDs matching the active section, so "NCh 1.333."
    inside section 7 stays as content, not a new observation.
    """
    split_items: List[Dict[str, Any]] = []
    current_section_num: Optional[int] = None

    for item in items:
        if item.get("kind") != "text":
            split_items.append(item)
            continue

        text = item.get("text", "")
        m_sec1 = RE_SEC1.match(text)
        if m_sec1 and prefix_depth(m_sec1.group(1)) == 1:
            sec_name = m_sec1.group(2).strip()
            if sec_name and not re.match(r"^\d", sec_name):
                current_section_num = section_number_from_text(m_sec1.group(1))

        parts = _split_text_on_embedded_observations(text, current_section_num)
        if len(parts) == 1:
            split_items.append(item)
            continue

        for part in parts:
            cloned = dict(item)
            cloned["text"] = part
            cloned["prefix"] = get_prefix(part)
            split_items.append(cloned)

    return split_items


def detect_requirement_types(text: str) -> List[str]:
    t = (text or "").lower()
    t = (
        t.replace("Ã¡", "a")
        .replace("Ã©", "e")
        .replace("Ã­", "i")
        .replace("Ã³", "o")
        .replace("Ãº", "u")
        .replace("Ã±", "n")
    )

    words = re.findall(r"[a-z]+", t)

    roots = {
        "solicitar": ["solicit"],
        "actualizar": ["actualiz"],
        "aclarar": ["aclar"],
        "rectificar": ["rectific"],
        "ampliar": ["ampli"],
        "informar": ["inform"],
        "justificar": ["justific"],
        "presentar": ["present"],
        "incorporar": ["incorpor"],
        "complementar": ["complement"],
        "especificar": ["especific"],
        "adjuntar": ["adjunt"],
        "verificar": ["verific"],
        "evaluar": ["evalu"],
        "revisar": ["revis"],
        "mantener": ["manten", "mantuv", "mantend"],
        "indicar": ["indic"],
        "senalar": ["senal"],
        "definir": ["defin"],
        "describir": ["describ"],
        "identificar": ["identific"],
        "corroborar": ["corrobor"],
        "remitir": ["remit"],
        "requerir": ["requier", "requer"],
        "reiterar": ["reiter"],
        "deber": ["deber", "deba", "debe"],
    }

    found = []
    for label, stems in roots.items():
        if any(any(word.startswith(stem) for stem in stems) for word in words):
            found.append(label)

    return sorted(set(found))


def normalizar_basico(text: str) -> str:
    t = (text or "").lower()
    return (
        t.replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
        .replace("Ã¡", "a")
        .replace("Ã©", "e")
        .replace("Ã­", "i")
        .replace("Ã³", "o")
        .replace("Ãº", "u")
        .replace("Ã±", "n")
    )


def is_level2_observation_text(text: str) -> bool:
    m = RE_SEC2.match(text or "")
    body = m.group(2).strip() if m else ""
    body_norm = normalizar_basico(body)

    if (
        "siguientes observaciones" in body_norm
        or (body.endswith(":") and len(detect_requirement_types(text)) == 0)
    ):
        return False

    if len(detect_requirement_types(text)) > 0:
        return True

    if not m:
        return False

    return bool(
        re.match(
            r"^(Respecto(?:\s+de|\s+del|\s+a)?|"
            r"Con\s+respecto(?:\s+a)?|"
            r"En\s+relaci[oó]n(?:\s+con|\s+a)?|"
            r"En\s+cuanto(?:\s+a)?|"
            r"Sobre)\b",
            body,
            flags=re.IGNORECASE,
        )
    )


# =========================================================
# EXTRACCIÃ“N DE TEXTO
# =========================================================

def extract_lines(page: fitz.Page) -> List[Dict[str, Any]]:
    raw = page.get_text("dict")
    lines = []

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            text = "".join(span.get("text", "") for span in spans)
            text = norm(text)
            if not text:
                continue

            x0 = min(s["bbox"][0] for s in spans)
            y0 = min(s["bbox"][1] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            y1 = max(s["bbox"][3] for s in spans)

            sizes = [s.get("size", 0) for s in spans if s.get("size")]
            avg_size = sum(sizes) / len(sizes) if sizes else 0

            lines.append(
                {
                    "text": text,
                    "bbox": (x0, y0, x1, y1),
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "size": avg_size,
                }
            )

    lines.sort(key=lambda r: (round(r["y0"], 1), round(r["x0"], 1)))
    return lines


def compute_typical_gap(lines: List[Dict[str, Any]]) -> float:
    gaps = []
    for i in range(1, len(lines)):
        gap = lines[i]["y0"] - lines[i - 1]["y1"]
        if 0 <= gap <= 30:
            gaps.append(gap)
    if not gaps:
        return 7.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def should_break(prev_line: Dict[str, Any], cur_line: Dict[str, Any], typical_gap: float) -> bool:
    prev_text = prev_line["text"]
    cur_text = cur_line["text"]

    cur_prefix = get_prefix(cur_text)
    if cur_prefix:
        return True

    if RE_FIG.match(cur_text) or RE_TABLA.match(cur_text):
        return True

    gap = cur_line["y0"] - prev_line["y1"]
    indent_diff = abs(cur_line["x0"] - prev_line["x0"])
    size_diff = abs((cur_line.get("size") or 0) - (prev_line.get("size") or 0))

    prev_prefix = get_prefix(prev_text)
    if prev_prefix and not cur_prefix and gap <= typical_gap * 2.5:
        return False

    if gap > typical_gap * 2.8:
        return True

    if indent_diff > 35 and gap > typical_gap * 1.4:
        return True

    if size_diff > 1.5 and gap > typical_gap * 1.2:
        return True

    return False


def build_block(lines: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not lines:
        return None

    text = one_line("\n".join(l["text"] for l in lines))
    if not text or is_footer_or_noise(text):
        return None

    bbox = lines[0]["bbox"]
    for l in lines[1:]:
        bbox = bbox_union(bbox, l["bbox"])

    prefix = get_prefix(text)

    return {
        "kind": "text",
        "bbox": bbox,
        "text": text,
        "prefix": prefix,
    }


def group_lines(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not lines:
        return []

    typical_gap = compute_typical_gap(lines)
    groups = []
    current = [lines[0]]

    for line in lines[1:]:
        if should_break(current[-1], line, typical_gap):
            block = build_block(current)
            if block:
                groups.append(block)
            current = [line]
        else:
            current.append(line)

    block = build_block(current)
    if block:
        groups.append(block)

    return groups


def should_merge_blocks(prev_block: Dict[str, Any], cur_block: Dict[str, Any]) -> bool:
    if prev_block["kind"] != "text" or cur_block["kind"] != "text":
        return False

    prev_text = prev_block["text"]
    cur_text = cur_block["text"]

    cur_prefix = get_prefix(cur_text)
    if cur_prefix:
        return False

    if RE_FIG.match(cur_text) or RE_TABLA.match(cur_text):
        return False

    gap = cur_block["bbox"][1] - prev_block["bbox"][3]
    left_diff = abs(cur_block["bbox"][0] - prev_block["bbox"][0])

    prev_soft = not re.search(r"[.:;!?]\s*$", prev_text)
    cur_lower = bool(re.match(r"^[a-zÃ¡Ã©Ã­Ã³ÃºÃ±0-9\(\[]", cur_text))

    prev_prefix = get_prefix(prev_text)
    if prev_prefix and not cur_prefix and gap <= 18:
        if cur_lower or gap <= 4:
            return True

    if gap <= 12 and left_diff <= 35 and (prev_soft or cur_lower):
        return True

    return False


def merge_adjacent_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not blocks:
        return []

    merged = [blocks[0]]
    for block in blocks[1:]:
        prev = merged[-1]
        if should_merge_blocks(prev, block):
            prev["text"] = one_line(prev["text"] + " " + block["text"])
            prev["bbox"] = bbox_union(prev["bbox"], block["bbox"])
            prev["prefix"] = prev.get("prefix") or block.get("prefix")
        else:
            merged.append(block)
    return merged


# =========================================================
# DETECCIÃ“N DE TABLAS (bbox) â€” sin cambios
# =========================================================

MIN_LINE_LEN = 18.0
THIN_MAX = 2.5
MIN_HLINES = 2
MIN_VLINES = 2
MIN_TABLE_AREA = 2500.0
MIN_TABLE_WIDTH = 80.0
MIN_TABLE_HEIGHT = 40.0
MERGE_GAP = 8.0
LINE_ALIGN_TOL = 2.0
MIN_TABLE_AREA_FINDER = 1800.0
MIN_TEXT_CHARS_IN_TABLE = 8
TABLE_BBOX_PAD = 3.0
TABLE_DUP_IOU = 0.70


def get_text_in_rect(page: fitz.Page, rect: fitz.Rect) -> str:
    txt = page.get_text("text", clip=rect)
    return " ".join(txt.split()).strip()


def has_enough_text_for_table(page: fitz.Page, rect: fitz.Rect) -> bool:
    txt = get_text_in_rect(page, rect)
    return len(txt) >= MIN_TEXT_CHARS_IN_TABLE


def normalize_line_rect(r: fitz.Rect) -> fitz.Rect:
    x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1

    if abs(y1 - y0) < 0.5:
        cy = (y0 + y1) / 2.0
        return fitz.Rect(x0, cy - 0.5, x1, cy + 0.5)

    if abs(x1 - x0) < 0.5:
        cx = (x0 + x1) / 2.0
        return fitz.Rect(cx - 0.5, y0, cx + 0.5, y1)

    return r


def _tables_from_vector_drawings(page: fitz.Page) -> List[fitz.Rect]:
    drawings = page.get_drawings()
    h_lines: List[fitz.Rect] = []
    v_lines: List[fitz.Rect] = []

    for d in drawings:
        for it in d.get("items", []):
            op = it[0]

            if op == "l":
                p1, p2 = it[1], it[2]
                x1, y1 = float(p1.x), float(p1.y)
                x2, y2 = float(p2.x), float(p2.y)
                dx, dy = abs(x2 - x1), abs(y2 - y1)

                if dx >= MIN_LINE_LEN and dy <= LINE_ALIGN_TOL:
                    r = fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                    h_lines.append(normalize_line_rect(r))

                elif dy >= MIN_LINE_LEN and dx <= LINE_ALIGN_TOL:
                    r = fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                    v_lines.append(normalize_line_rect(r))

            elif op == "re":
                r = fitz.Rect(it[1])
                w, h = abs(r.x1 - r.x0), abs(r.y1 - r.y0)

                if h <= THIN_MAX and w >= MIN_LINE_LEN:
                    h_lines.append(normalize_line_rect(r))
                elif w <= THIN_MAX and h >= MIN_LINE_LEN:
                    v_lines.append(normalize_line_rect(r))
                elif w >= MIN_TABLE_WIDTH and h >= MIN_TABLE_HEIGHT:
                    top = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + 1)
                    bot = fitz.Rect(r.x0, r.y1 - 1, r.x1, r.y1)
                    lef = fitz.Rect(r.x0, r.y0, r.x0 + 1, r.y1)
                    rig = fitz.Rect(r.x1 - 1, r.y0, r.x1, r.y1)
                    h_lines.extend([normalize_line_rect(top), normalize_line_rect(bot)])
                    v_lines.extend([normalize_line_rect(lef), normalize_line_rect(rig)])

    if len(h_lines) < MIN_HLINES or len(v_lines) < MIN_VLINES:
        return []

    h_merged = merge_rects(h_lines, gap=2.0)
    v_merged = merge_rects(v_lines, gap=2.0)

    all_rects = h_merged + v_merged
    groups = merge_rects(all_rects, gap=MERGE_GAP)

    out = []
    for g in groups:
        if g.width < MIN_TABLE_WIDTH or g.height < MIN_TABLE_HEIGHT:
            continue
        if rect_area(g) < MIN_TABLE_AREA:
            continue
        out.append(g)

    return deduplicate_rects(out, iou_thr=TABLE_DUP_IOU)


def _tables_from_pymupdf_find_tables(page: fitz.Page) -> List[fitz.Rect]:
    rects: List[fitz.Rect] = []
    finder = getattr(page, "find_tables", None)
    if not callable(finder):
        return rects

    try:
        res = finder()
    except Exception:
        return rects

    tables = getattr(res, "tables", None) or []
    for t in tables:
        bbox = getattr(t, "bbox", None)
        if bbox is None:
            continue
        r = fitz.Rect(bbox)
        if rect_area(r) >= MIN_TABLE_AREA_FINDER and r.width >= MIN_TABLE_WIDTH and r.height >= MIN_TABLE_HEIGHT:
            rects.append(r)

    return deduplicate_rects(rects, iou_thr=TABLE_DUP_IOU)


def extract_table_candidates(page: fitz.Page) -> List[Dict[str, Any]]:
    vec = _tables_from_vector_drawings(page)
    finder_rects = _tables_from_pymupdf_find_tables(page)

    raw: List[Tuple[fitz.Rect, str]] = []
    raw.extend((r, "vector") for r in vec)
    raw.extend((r, "find_tables") for r in finder_rects)

    if not raw:
        return []

    merged_candidates: List[Dict[str, Any]] = []

    used = [False] * len(raw)
    for i, (ri, mi) in enumerate(raw):
        if used[i]:
            continue

        cluster_rects = [ri]
        methods = {mi}
        used[i] = True
        changed = True

        while changed:
            changed = False
            current_union = cluster_rects[0]
            for rr in cluster_rects[1:]:
                current_union = union_rect(current_union, rr)

            for j, (rj, mj) in enumerate(raw):
                if used[j]:
                    continue
                if rects_touch_or_overlap(current_union, rj, tol=MERGE_GAP * 2) or iou_rect(current_union, rj) > 0.10:
                    cluster_rects.append(rj)
                    methods.add(mj)
                    used[j] = True
                    changed = True

        bbox = cluster_rects[0]
        for rr in cluster_rects[1:]:
            bbox = union_rect(bbox, rr)

        merged_candidates.append({
            "bbox": bbox,
            "methods": sorted(methods),
        })

    out: List[Dict[str, Any]] = []
    for item in merged_candidates:
        r = item["bbox"]
        if rect_area(r) < MIN_TABLE_AREA_FINDER:
            continue
        if r.width < MIN_TABLE_WIDTH or r.height < MIN_TABLE_HEIGHT:
            continue
        if is_inside_header_footer(r, page.rect):
            continue
        if not has_enough_text_for_table(page, r):
            continue

        out.append({
            "bbox": expand_rect(r, TABLE_BBOX_PAD, page.rect),
            "methods": item["methods"],
        })

    final_rects = deduplicate_rects([x["bbox"] for x in out], iou_thr=TABLE_DUP_IOU)
    final_out: List[Dict[str, Any]] = []

    for fr in final_rects:
        methods = set()
        for item in out:
            if iou_rect(fr, item["bbox"]) > 0.60 or rects_touch_or_overlap(fr, item["bbox"], tol=3):
                methods.update(item["methods"])
        final_out.append({
            "bbox": fr,
            "methods": sorted(methods) if methods else ["unknown"],
        })

    return final_out


def find_table_caption(page_text_blocks: List[Dict[str, Any]], table_rect: fitz.Rect) -> Optional[str]:
    best = None
    best_score = 1e9

    for txt in page_text_blocks:
        t = txt["text"].strip()
        if not RE_TABLA.match(t):
            continue

        txt_rect = fitz.Rect(txt["bbox"])

        horizontal_overlap = max(
            0.0,
            min(table_rect.x1, txt_rect.x1) - max(table_rect.x0, txt_rect.x0),
        )
        min_width = max(1.0, min(table_rect.width, txt_rect.width))
        overlap_ratio = horizontal_overlap / min_width

        if overlap_ratio < 0.15:
            continue

        if txt_rect.y0 >= table_rect.y1:
            dist = txt_rect.y0 - table_rect.y1
            bias = 20
        elif table_rect.y0 >= txt_rect.y1:
            dist = table_rect.y0 - txt_rect.y1
            bias = 0
        else:
            dist = 0
            bias = 30

        if dist > 120:
            continue

        score = dist + bias
        if score < best_score:
            best = t
            best_score = score

    return best


def save_bbox_screenshot(
    doc: fitz.Document,
    page_index0: int,
    bbox: fitz.Rect,
    out_dir: str,
    fname: str,
    dpi: int = 220,
) -> str:
    page = doc[page_index0]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=bbox, alpha=False)

    ensure_dir(out_dir)
    out_path = os.path.join(out_dir, fname)
    pix.save(out_path)
    return out_path


# =========================================================
# EXTRACCIÃ“N ESTRUCTURADA DE TABLAS â€” MÃ‰TODO 1: pdfplumber
# =========================================================

def _pdfplumber_extract_table(
    pdf_path: str,
    page_index0: int,
    rect: fitz.Rect,
) -> Optional[Dict[str, Any]]:
    """
    Usa pdfplumber para extraer la estructura de celdas de una tabla.
    pdfplumber es mucho mejor que PyMuPDF find_tables para tablas con
    lÃ­neas vectoriales (el caso tÃ­pico de ICSARA/SEIA).
    """
    if not PDFPLUMBER_AVAILABLE:
        return None

    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_index0 >= len(pdf.pages):
                return None

            page = pdf.pages[page_index0]

            # Crop al Ã¡rea de la tabla (pdfplumber usa mismas coordenadas PDF)
            crop_bbox = (
                float(rect.x0),
                float(rect.y0),
                float(rect.x1),
                float(rect.y1),
            )
            cropped = page.crop(crop_bbox)

            # ConfiguraciÃ³n de extracciÃ³n optimizada para ICSARA
            table_settings = {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 5,
                "join_tolerance": 5,
                "edge_min_length": 15,
                "min_words_vertical": 1,
                "min_words_horizontal": 1,
                "intersection_tolerance": 8,
            }

            tables = cropped.extract_tables(table_settings)

            if not tables:
                # Fallback: probar con estrategia "text" para lÃ­neas
                table_settings["vertical_strategy"] = "lines_strict"
                table_settings["horizontal_strategy"] = "lines_strict"
                tables = cropped.extract_tables(table_settings)

            if not tables:
                # Ãšltimo fallback: estrategia mixta
                table_settings["vertical_strategy"] = "text"
                table_settings["horizontal_strategy"] = "lines"
                tables = cropped.extract_tables(table_settings)

            if not tables:
                return None

            # Tomar la tabla mÃ¡s grande (mÃ¡s celdas)
            best_table = max(tables, key=lambda t: len(t) * len(t[0]) if t and t[0] else 0)

            if not best_table or len(best_table) < 1:
                return None

            # Limpiar celdas
            cleaned_rows = []
            max_cols = 0

            for row in best_table:
                cleaned_row = []
                for cell in row:
                    if cell is None or str(cell).strip() == "":
                        cleaned_row.append(None)
                    else:
                        cell_text = " ".join(str(cell).split())
                        cleaned_row.append(cell_text if cell_text else None)
                cleaned_rows.append(cleaned_row)
                max_cols = max(max_cols, len(cleaned_row))

            if max_cols == 0:
                return None

            return {
                "rows": cleaned_rows,
                "num_rows": len(cleaned_rows),
                "num_cols": max_cols,
                "method": "pdfplumber",
            }

    except Exception as e:
        # Debug: descomentar para ver errores
        # print(f"  [pdfplumber error] page {page_index0+1}: {e}")
        return None


# =========================================================
# EXTRACCIÃ“N ESTRUCTURADA DE TABLAS â€” MÃ‰TODO 2: PyMuPDF
# =========================================================

def _pymupdf_extract_table(
    page: fitz.Page,
    rect: fitz.Rect,
) -> Optional[Dict[str, Any]]:
    """
    Usa PyMuPDF find_tables().extract() â€” mÃ©todo original.
    """
    finder = getattr(page, "find_tables", None)
    if not callable(finder):
        return None

    try:
        res = finder()
    except Exception:
        return None

    tables = getattr(res, "tables", None) or []

    best_table = None
    best_iou = 0.0
    for t in tables:
        bbox = getattr(t, "bbox", None)
        if bbox is None:
            continue
        t_rect = fitz.Rect(bbox)
        score = iou_rect(rect, t_rect)
        if score > best_iou:
            best_iou = score
            best_table = t

    if best_table is None or best_iou < 0.25:
        return None

    try:
        raw_rows = best_table.extract()
    except Exception:
        return None

    if not raw_rows:
        return None

    cleaned_rows = []
    max_cols = 0

    for row in raw_rows:
        cleaned_row = []
        for cell in row:
            if cell is None:
                cleaned_row.append(None)
            else:
                cell_text = " ".join(str(cell).split())
                cleaned_row.append(cell_text if cell_text else None)
        cleaned_rows.append(cleaned_row)
        max_cols = max(max_cols, len(cleaned_row))

    return {
        "rows": cleaned_rows,
        "num_rows": len(cleaned_rows),
        "num_cols": max_cols,
        "method": "pymupdf_find_tables",
    }


# =========================================================
# EXTRACCIÃ“N ESTRUCTURADA DE TABLAS â€” MÃ‰TODO 3: HeurÃ­stico
# Reconstruye filas/columnas a partir de lÃ­neas vectoriales
# y texto con coordenadas (dict blocks).
# =========================================================

def _heuristic_extract_table(
    page: fitz.Page,
    rect: fitz.Rect,
) -> Optional[Dict[str, Any]]:
    """
    MÃ©todo heurÃ­stico: detecta las lÃ­neas horizontales y verticales dentro
    del bbox de la tabla para determinar la grilla de celdas, luego asigna
    el texto de cada span a la celda correspondiente.
    """
    # 1. Recolectar lÃ­neas H y V dentro del rect
    drawings = page.get_drawings()
    h_ys: List[float] = []
    v_xs: List[float] = []

    pad = 3.0
    clip = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)

    for d in drawings:
        for it in d.get("items", []):
            op = it[0]
            if op == "l":
                p1, p2 = it[1], it[2]
                x1f, y1f = float(p1.x), float(p1.y)
                x2f, y2f = float(p2.x), float(p2.y)
                mid_r = fitz.Rect(
                    min(x1f, x2f), min(y1f, y2f),
                    max(x1f, x2f), max(y1f, y2f),
                )
                if not rects_touch_or_overlap(mid_r, clip, tol=2):
                    continue

                dx, dy = abs(x2f - x1f), abs(y2f - y1f)
                if dx >= 15 and dy <= 3:
                    h_ys.append((y1f + y2f) / 2.0)
                elif dy >= 15 and dx <= 3:
                    v_xs.append((x1f + x2f) / 2.0)

            elif op == "re":
                r = fitz.Rect(it[1])
                if not rects_touch_or_overlap(r, clip, tol=2):
                    continue
                w, h = r.width, r.height
                if h <= 3 and w >= 15:
                    h_ys.append((r.y0 + r.y1) / 2.0)
                elif w <= 3 and h >= 15:
                    v_xs.append((r.x0 + r.x1) / 2.0)
                elif w >= 30 and h >= 20:
                    h_ys.extend([r.y0, r.y1])
                    v_xs.extend([r.x0, r.x1])

    # TambiÃ©n considerar los bordes del rect como lÃ­neas
    h_ys.extend([rect.y0, rect.y1])
    v_xs.extend([rect.x0, rect.x1])

    # Deduplicar con tolerancia
    def dedup_coords(coords: List[float], tol: float = 4.0) -> List[float]:
        if not coords:
            return []
        coords = sorted(set(coords))
        result = [coords[0]]
        for c in coords[1:]:
            if c - result[-1] > tol:
                result.append(c)
            else:
                result[-1] = (result[-1] + c) / 2.0
        return result

    h_ys = dedup_coords(h_ys, tol=4.0)
    v_xs = dedup_coords(v_xs, tol=4.0)

    if len(h_ys) < 2 or len(v_xs) < 2:
        return None

    num_rows = len(h_ys) - 1
    num_cols = len(v_xs) - 1

    if num_rows < 1 or num_cols < 1:
        return None

    # 2. Crear grilla de celdas vacÃ­as
    grid: List[List[List[str]]] = [[[] for _ in range(num_cols)] for _ in range(num_rows)]

    # 3. Extraer words/spans con coordenadas y asignar a celdas
    raw_dict = page.get_text("dict", clip=rect)
    for block in raw_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text", "") or "").strip()
                if not txt:
                    continue
                sx0 = span["bbox"][0]
                sy0 = span["bbox"][1]
                sx1 = span["bbox"][2]
                sy1 = span["bbox"][3]
                cx = (sx0 + sx1) / 2.0
                cy = (sy0 + sy1) / 2.0

                # Encontrar fila
                row_idx = None
                for ri in range(num_rows):
                    if h_ys[ri] - 2 <= cy <= h_ys[ri + 1] + 2:
                        row_idx = ri
                        break
                if row_idx is None:
                    # Buscar la fila mÃ¡s cercana
                    best_ri = 0
                    best_dist = abs(cy - (h_ys[0] + h_ys[1]) / 2.0)
                    for ri in range(num_rows):
                        mid = (h_ys[ri] + h_ys[ri + 1]) / 2.0
                        d = abs(cy - mid)
                        if d < best_dist:
                            best_dist = d
                            best_ri = ri
                    row_idx = best_ri

                # Encontrar columna
                col_idx = None
                for ci in range(num_cols):
                    if v_xs[ci] - 2 <= cx <= v_xs[ci + 1] + 2:
                        col_idx = ci
                        break
                if col_idx is None:
                    best_ci = 0
                    best_dist = abs(cx - (v_xs[0] + v_xs[1]) / 2.0)
                    for ci in range(num_cols):
                        mid = (v_xs[ci] + v_xs[ci + 1]) / 2.0
                        d = abs(cx - mid)
                        if d < best_dist:
                            best_dist = d
                            best_ci = ci
                    col_idx = best_ci

                if 0 <= row_idx < num_rows and 0 <= col_idx < num_cols:
                    grid[row_idx][col_idx].append(txt)

    # 4. Convertir grilla a rows
    rows = []
    for ri in range(num_rows):
        row = []
        for ci in range(num_cols):
            cell_parts = grid[ri][ci]
            if cell_parts:
                cell_text = " ".join(cell_parts)
                cell_text = re.sub(r"\s{2,}", " ", cell_text).strip()
                row.append(cell_text if cell_text else None)
            else:
                row.append(None)
        rows.append(row)

    # Validar: al menos alguna celda con contenido
    has_content = any(
        any(cell is not None for cell in row)
        for row in rows
    )
    if not has_content:
        return None

    return {
        "rows": rows,
        "num_rows": num_rows,
        "num_cols": num_cols,
        "method": "heuristic_grid",
    }


# =========================================================
# FUNCIONES AUXILIARES OCR (Tesseract)
# =========================================================

def _safe_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def ocr_words_from_image(image_path: str) -> List[Dict[str, Any]]:
    """Extrae palabras con coordenadas desde una imagen usando Tesseract."""
    if not OCR_TABLES_FROM_IMAGES or not TESSERACT_AVAILABLE:
        return []

    try:
        img = Image.open(image_path)
        data = pytesseract.image_to_data(
            img,
            lang="spa+eng",
            output_type=pytesseract.Output.DICT,
            config="--psm 6"
        )
    except Exception:
        return []

    words = []
    n = len(data.get("text", []))
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        conf_raw = data.get("conf", [])[i] if i < len(data.get("conf", [])) else -1
        try:
            conf = float(conf_raw)
        except Exception:
            conf = -1.0

        if not txt:
            continue
        if conf < 0:
            continue

        x = _safe_int(data["left"][i])
        y = _safe_int(data["top"][i])
        w = _safe_int(data["width"][i])
        h = _safe_int(data["height"][i])

        words.append({
            "text": txt,
            "x0": x,
            "y0": y,
            "x1": x + w,
            "y1": y + h,
            "width": w,
            "height": h,
            "conf": conf,
        })

    return words


def group_words_into_rows(words: List[Dict[str, Any]], y_tol: int = 12) -> List[List[Dict[str, Any]]]:
    """Agrupa palabras OCR en filas por proximidad vertical."""
    if not words:
        return []

    words = sorted(words, key=lambda w: (w["y0"], w["x0"]))
    rows: List[List[Dict[str, Any]]] = []

    for w in words:
        placed = False
        cy = (w["y0"] + w["y1"]) / 2.0

        for row in rows:
            row_centers = [((r["y0"] + r["y1"]) / 2.0) for r in row]
            row_center = sum(row_centers) / len(row_centers)
            if abs(cy - row_center) <= y_tol:
                row.append(w)
                placed = True
                break

        if not placed:
            rows.append([w])

    for row in rows:
        row.sort(key=lambda w: w["x0"])

    rows.sort(key=lambda row: min(w["y0"] for w in row))
    return rows


def infer_column_gaps(rows: List[List[Dict[str, Any]]], min_gap: int = 25) -> List[int]:
    """Detecta separadores de columna por gaps horizontales entre palabras."""
    gaps = []

    for row in rows:
        if len(row) < 2:
            continue
        for i in range(1, len(row)):
            gap = row[i]["x0"] - row[i - 1]["x1"]
            if gap >= min_gap:
                split_x = int((row[i]["x0"] + row[i - 1]["x1"]) / 2.0)
                gaps.append(split_x)

    if not gaps:
        return []

    gaps.sort()
    merged = [gaps[0]]

    for g in gaps[1:]:
        if abs(g - merged[-1]) <= 20:
            merged[-1] = int((merged[-1] + g) / 2.0)
        else:
            merged.append(g)

    return merged


def assign_word_to_column(word: Dict[str, Any], col_splits: List[int]) -> int:
    """Asigna una palabra a su columna segÃºn los separadores detectados."""
    cx = (word["x0"] + word["x1"]) / 2.0
    col = 0
    for split_x in col_splits:
        if cx > split_x:
            col += 1
    return col


# =========================================================
# EXTRACCIÃ“N ESTRUCTURADA â€” MÃ‰TODO 4: OCR desde screenshot
# Digitaliza la imagen PNG generada del recorte de la tabla.
# =========================================================

def _ocr_extract_table(image_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Usa Tesseract OCR sobre el screenshot PNG de la tabla para
    reconstruir filas y columnas a partir de las coordenadas de
    cada palabra detectada.
    """
    if not image_path or not os.path.isfile(image_path):
        return None

    if not TESSERACT_AVAILABLE:
        return None

    # 1. OCR: obtener palabras con coordenadas
    words = ocr_words_from_image(image_path)
    if not words:
        return None

    # 2. Agrupar palabras en filas por proximidad vertical
    rows_of_words = group_words_into_rows(words, y_tol=12)
    if not rows_of_words:
        return None

    # 3. Inferir separadores de columna por gaps horizontales
    col_splits = infer_column_gaps(rows_of_words, min_gap=25)

    # 4. Construir matriz de celdas
    result_rows: List[List[Optional[str]]] = []
    max_cols = 0

    for row in rows_of_words:
        cols: Dict[int, List[str]] = defaultdict(list)

        for w in row:
            col_idx = assign_word_to_column(w, col_splits)
            cols[col_idx].append(w["text"])

        max_col = max(cols.keys()) if cols else -1
        row_cells: List[Optional[str]] = []

        for c in range(max_col + 1):
            cell_text = " ".join(cols.get(c, []))
            cell_text = re.sub(r"\s{2,}", " ", cell_text).strip()
            row_cells.append(cell_text if cell_text else None)

        # Quitar celdas vacÃ­as del final
        while row_cells and row_cells[-1] is None:
            row_cells.pop()

        if row_cells:
            result_rows.append(row_cells)
            max_cols = max(max_cols, len(row_cells))

    if not result_rows or max_cols == 0:
        return None

    # Normalizar: todas las filas con mismo nÃºmero de columnas
    for i in range(len(result_rows)):
        while len(result_rows[i]) < max_cols:
            result_rows[i].append(None)

    # Validar que hay contenido real
    has_content = any(
        any(cell is not None for cell in row)
        for row in result_rows
    )
    if not has_content:
        return None

    return {
        "rows": result_rows,
        "num_rows": len(result_rows),
        "num_cols": max_cols,
        "method": "ocr_screenshot",
    }


# =========================================================
# FUNCIÃ“N PRINCIPAL DE EXTRACCIÃ“N ESTRUCTURADA
# Encadena: HeurÃ­stico â†’ PyMuPDF â†’ pdfplumber â†’ OCR
# =========================================================

def simplify_table_data(table_data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Post-procesa la matriz cruda:
    1. Fusiona filas de continuaciÃ³n (col 0 == None â†’ misma celda lÃ³gica).
    2. Elimina columnas que son null en todas las filas.
    """
    if not table_data or not table_data.get("rows"):
        return table_data

    rows = table_data["rows"]
    num_cols = table_data.get("num_cols", 0)
    method = table_data.get("method", "unknown")
    if num_cols == 0:
        return table_data

    # --- 1. Fusionar filas de continuaciÃ³n ---
    merged: List[List[Optional[str]]] = []
    for row in rows:
        padded: List[Optional[str]] = list(row) + [None] * (num_cols - len(row))

        if merged and padded[0] is None:
            prev = merged[-1]
            for c in range(num_cols):
                if padded[c] is not None:
                    if prev[c] is not None:
                        prev[c] = prev[c].rstrip() + " " + padded[c].lstrip()
                    else:
                        prev[c] = padded[c]
        else:
            merged.append(list(padded))

    # --- 2. Eliminar columnas totalmente null ---
    cols_to_keep = [
        c for c in range(num_cols)
        if any(row[c] is not None for row in merged)
    ]

    final_rows = [
        [row[c] for c in cols_to_keep]
        for row in merged
    ]

    result = {
        "rows": final_rows,
        "num_rows": len(final_rows),
        "num_cols": len(cols_to_keep),
        "method": method,
    }

    # ValidaciÃ³n: si quedÃ³ solo 1 fila con 1 columna, descartar
    if result["num_rows"] <= 1 and result["num_cols"] <= 1:
        return None

    return result


def extract_table_structured(
    page: fitz.Page,
    rect: fitz.Rect,
    pdf_path: str,
    page_index0: int,
    image_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Intenta extraer la estructura de celdas usando 4 mÃ©todos en cascada:
    1. HeurÃ­stico basado en lÃ­neas vectoriales + texto con coordenadas
    2. PyMuPDF find_tables (rÃ¡pido, nativo)
    3. pdfplumber (robusto)
    4. OCR desde screenshot PNG de la tabla
    """

    # --- MÃ©todo 1: HeurÃ­stico ---
    result = _heuristic_extract_table(page, rect)
    if result:
        simplified = simplify_table_data(result)
        if simplified and simplified["num_rows"] >= 2:
            return simplified

    # --- MÃ©todo 2: PyMuPDF find_tables ---
    result = _pymupdf_extract_table(page, rect)
    if result:
        simplified = simplify_table_data(result)
        if simplified and simplified["num_rows"] >= 2:
            return simplified

    # --- MÃ©todo 3: pdfplumber ---
    result = _pdfplumber_extract_table(pdf_path, page_index0, rect)
    if result:
        simplified = simplify_table_data(result)
        if simplified and simplified["num_rows"] >= 2:
            return simplified

    # --- MÃ©todo 4: OCR desde screenshot ---
    result = _ocr_extract_table(image_path)
    if result:
        simplified = simplify_table_data(result)
        if simplified:
            return simplified

    return None


def table_data_to_markdown(table_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Renderiza table_data como string Markdown."""
    if not table_data or not table_data.get("rows"):
        return None

    rows = table_data["rows"]
    num_cols = table_data.get("num_cols", 0)
    if num_cols == 0:
        return None

    lines = []
    for i, row in enumerate(rows):
        padded = list(row) + [None] * (num_cols - len(row))
        cells = [str(c) if c is not None else "" for c in padded]
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * num_cols) + "|")

    return "\n".join(lines)


# =========================================================
# FALLBACK: reconstruir Markdown desde texto vectorial plano
# cuando ningÃºn mÃ©todo estructurado funciona.
# =========================================================

def _fallback_text_to_markdown(vectorial_text: str) -> Optional[str]:
    """
    Intenta generar un Markdown mÃ­nimo a partir del texto plano
    extraÃ­do del bbox de la tabla. Separa por lÃ­neas y busca
    patrones tabulares.
    """
    if not vectorial_text or not vectorial_text.strip():
        return None

    lines = [l.strip() for l in vectorial_text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    # Si parece que hay un patrÃ³n tabular (lÃ­neas con longitudes similares),
    # renderizar como tabla de 1 columna al menos
    md_lines = []
    for i, line in enumerate(lines):
        md_lines.append(f"| {line} |")
        if i == 0:
            md_lines.append("|---|")

    return "\n".join(md_lines)


# =========================================================
# EXTRACCIÃ“N DE TABLAS (principal)
# =========================================================

def extract_tables(
    doc: fitz.Document,
    tables_dir: str,
    page_text_blocks_by_page: Dict[int, List[Dict[str, Any]]],
    pdf_path: str,
) -> Tuple[Dict[int, List[Dict[str, Any]]], Dict[str, Any]]:
    result = defaultdict(list)

    tables_document: Dict[str, Any] = {
        "pdf_path": pdf_path,
        "tables_dir": tables_dir,
        "tables": [],
    }

    total = 0

    for pno in range(len(doc)):
        page = doc[pno]
        page_no = pno + 1
        text_blocks = page_text_blocks_by_page.get(page_no, [])

        candidates = extract_table_candidates(page)

        for idx, cand in enumerate(candidates, start=1):
            rect = cand["bbox"]
            methods = cand["methods"]

            # Screenshot
            file_name = f"page_{page_no:03d}_table_{idx:03d}.png"
            file_path = None
            try:
                file_path = save_bbox_screenshot(
                    doc=doc,
                    page_index0=pno,
                    bbox=rect,
                    out_dir=tables_dir,
                    fname=file_name,
                    dpi=220,
                )
            except Exception:
                file_path = None

            caption = find_table_caption(text_blocks, rect)

            # Texto vectorial directo
            vectorial_text = page.get_text("text", clip=rect).strip()

            # ===== ExtracciÃ³n estructurada con 4 mÃ©todos en cascada =====
            table_data = extract_table_structured(
                page=page,
                rect=rect,
                pdf_path=pdf_path,
                page_index0=pno,
                image_path=file_path,
            )

            # Markdown desde datos estructurados
            table_md = table_data_to_markdown(table_data)

            # Fallback: si no hay datos estructurados, generar Markdown
            # mÃ­nimo desde el texto vectorial
            if table_md is None and vectorial_text:
                table_md = _fallback_text_to_markdown(vectorial_text)

            # MÃ©todo de extracciÃ³n usado
            extraction_method = table_data.get("method", "none") if table_data else "text_only"

            table_item = {
                "kind": "table",
                "page": page_no,
                "bbox": rect_to_tuple(rect),
                "text": vectorial_text,
                "ocr_text": "",
                "table_data": table_data,
                "table_md": table_md,
                "table_file": file_path,
                "caption": caption,
                "detection_methods": methods,
                "extraction_method": extraction_method,
                "table_index_on_page": idx,
            }

            result[page_no].append(table_item)

            tables_document["tables"].append({
                "id": f"p{page_no:04d}_t{idx:02d}",
                "page": page_no,
                "table_index_on_page": idx,
                "caption": caption,
                "detection_methods": methods,
                "extraction_method": extraction_method,
                "bbox_pdf": rect_to_dict(rect),
                "image_path": file_path,
                "text_digital_pdf": vectorial_text,
                "table_md": table_md,
                "ocr_text_from_image": "",
            })

            total += 1

    tables_document["table_count"] = total
    tables_document["ocr_enabled"] = False
    tables_document["extraction_methods_available"] = [
        "pdfplumber" if PDFPLUMBER_AVAILABLE else "(pdfplumber no disponible)",
        "pymupdf_find_tables",
        "heuristic_grid",
        "text_only (fallback)",
    ]
    return result, tables_document


# =========================================================
# FIGURAS / IMÃGENES
# =========================================================

MIN_IMAGE_WIDTH = 60.0
MIN_IMAGE_HEIGHT = 60.0
MIN_IMAGE_AREA = 5000.0
IMAGE_DUP_IOU = 0.65
IMAGE_MERGE_GAP = 6.0
IMAGE_BBOX_PAD = 3.0


def image_is_probably_noise(rect: fitz.Rect, page_rect: fitz.Rect) -> bool:
    if is_inside_header_footer(rect, page_rect, header_ratio=0.08, footer_ratio=0.08):
        return True
    if rect.width < MIN_IMAGE_WIDTH:
        return True
    if rect.height < MIN_IMAGE_HEIGHT:
        return True
    if rect_area(rect) < MIN_IMAGE_AREA:
        return True
    return False


def collect_image_candidates(page: fitz.Page) -> List[fitz.Rect]:
    raw = page.get_text("dict")
    page_rect = page.rect
    candidates: List[fitz.Rect] = []

    for block in raw.get("blocks", []):
        if block.get("type") != 1:
            continue

        bbox = block.get("bbox")
        if not bbox:
            continue

        rect = fitz.Rect(bbox)

        if image_is_probably_noise(rect, page_rect):
            continue

        rect = expand_rect(rect, IMAGE_BBOX_PAD, page_rect)
        candidates.append(rect)

    if not candidates:
        return []

    candidates = merge_rects(candidates, gap=IMAGE_MERGE_GAP)
    candidates = deduplicate_rects(candidates, iou_thr=IMAGE_DUP_IOU)
    return candidates


def extract_images(page: fitz.Page, images_dir: str) -> List[Dict[str, Any]]:
    page_no = page.number + 1
    candidates = collect_image_candidates(page)
    images: List[Dict[str, Any]] = []

    for idx, rect in enumerate(candidates, start=1):
        file_name = f"page_{page_no:03d}_image_{idx:03d}.png"
        file_path = os.path.join(images_dir, file_name)

        try:
            pix = page.get_pixmap(
                matrix=fitz.Matrix(2, 2),
                clip=rect,
                alpha=False,
            )
            pix.save(file_path)
        except Exception:
            file_path = None

        images.append(
            {
                "kind": "image",
                "bbox": rect_to_tuple(rect),
                "file": file_path,
                "caption": None,
                "detection_method": "image_block_bbox",
                "width": round(float(rect.width), 2),
                "height": round(float(rect.height), 2),
                "area": round(float(rect_area(rect)), 2),
            }
        )

    return images


def attach_figure_captions(page_items: List[Dict[str, Any]]) -> None:
    text_blocks = [x for x in page_items if x["kind"] == "text"]
    image_blocks = [x for x in page_items if x["kind"] == "image"]

    for img in image_blocks:
        img_rect = fitz.Rect(img["bbox"])
        best = None
        best_score = 1e9

        for txt in text_blocks:
            tt = txt["text"].strip()
            if not RE_FIG.match(tt):
                continue

            txt_rect = fitz.Rect(txt["bbox"])

            horizontal_overlap = max(
                0.0,
                min(img_rect.x1, txt_rect.x1) - max(img_rect.x0, txt_rect.x0),
            )
            min_width = max(1.0, min(img_rect.width, txt_rect.width))
            overlap_ratio = horizontal_overlap / min_width

            if overlap_ratio < 0.20:
                continue

            if txt_rect.y0 >= img_rect.y1:
                dist = txt_rect.y0 - img_rect.y1
                bias = 0
            elif img_rect.y0 >= txt_rect.y1:
                dist = img_rect.y0 - txt_rect.y1
                bias = 10
            else:
                dist = 0
                bias = 30

            if dist > 120:
                continue

            score = dist + bias
            if score < best_score:
                best = txt
                best_score = score

        if best:
            img["caption"] = best["text"]


# =========================================================
# EXCLUSIÃ“N DE TEXTO DENTRO DE TABLAS
# =========================================================

def intersection_area(a, b):
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def area(b):
    return max(0.0, (b[2] - b[0])) * max(0.0, (b[3] - b[1]))


def text_block_belongs_to_table(text_block, table_block, min_overlap_ratio=0.01):
    if text_block["page"] != table_block["page"]:
        return False

    inter = intersection_area(text_block["bbox"], table_block["bbox"])
    if inter <= 0:
        return False

    text_area = area(text_block["bbox"])
    if text_area <= 0:
        return False

    overlap = inter / text_area
    return overlap >= min_overlap_ratio


def remove_table_text_blocks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tables_by_page = defaultdict(list)
    for item in items:
        if item["kind"] == "table":
            tables_by_page[item["page"]].append(item)

    cleaned = []
    for item in items:
        if item["kind"] != "text":
            cleaned.append(item)
            continue

        text = item.get("text", "").strip()

        if RE_OBS.match(text) or RE_SEC1.match(text) or RE_SEC2.match(text):
            cleaned.append(item)
            continue

        if RE_FIG.match(text) or RE_TABLA.match(text):
            cleaned.append(item)
            continue

        page_tables = tables_by_page.get(item["page"], [])
        inside_table = any(text_block_belongs_to_table(item, tb) for tb in page_tables)

        if not inside_table:
            cleaned.append(item)

    return cleaned


# =========================================================
# ASIGNACIÃ“N DE SECCIONES Y OBSERVACIONES
# =========================================================

def _resolve_pending_sections(
    obs_prefix: str,
    current_sec1_name: Optional[str],
    current_sec2_name: Optional[str],
    pending_sec1: Optional[Tuple[str, str]],
    pending_sec2: Optional[Tuple[str, str]],
) -> Tuple[Optional[str], Optional[str], Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """
    Cuando una nueva observaciÃ³n comienza, valida si los cambios de secciÃ³n
    pendientes son coherentes con el prefijo de la observaciÃ³n.
    Ej: si pending_sec1 = ("8.", "Plan de cumplimiento...") y la nueva obs
    es "8.1.1.", el prefijo 8 coincide â†’ se aplica el cambio.
    Si pending_sec1 = ("21.", "Skytanthus...") y la obs es "7.6.5.",
    el prefijo 21 â‰  7 â†’ se descarta (era un Ã­tem de lista).
    """
    obs_parts = obs_prefix.rstrip(".").split(".")

    # --- Resolver secciÃ³n 1 pendiente ---
    if pending_sec1 is not None and len(obs_parts) >= 1:
        try:
            obs_sec1_num = int(obs_parts[0])
            pending_sec1_num = int(pending_sec1[0].replace(".", ""))
            if obs_sec1_num == pending_sec1_num:
                # Confirmado: la observaciÃ³n pertenece a esta nueva secciÃ³n
                current_sec1_name = canonical_section_label(pending_sec1[0], pending_sec1[1])
                current_sec2_name = None
        except (ValueError, IndexError):
            pass
        pending_sec1 = None

    # --- Resolver secciÃ³n 2 pendiente ---
    if pending_sec2 is not None and len(obs_parts) >= 2:
        try:
            obs_sec2_prefix = f"{obs_parts[0]}.{obs_parts[1]}."
            if canonical_numbered_id(pending_sec2[0]) == canonical_numbered_id(obs_sec2_prefix):
                # Confirmado: la observaciÃ³n pertenece a esta nueva subsecciÃ³n
                current_sec2_name = canonical_section_label(pending_sec2[0], pending_sec2[1])
        except (ValueError, IndexError):
            pass
        pending_sec2 = None

    return current_sec1_name, current_sec2_name, pending_sec1, pending_sec2


def detect_sections_and_observations(items: List[Dict[str, Any]]) -> None:
    current_sec1_name = None
    current_sec2_name = None
    current_obs = None

    # Secciones pendientes: se guardan cuando un cambio de secciÃ³n ocurre
    # dentro de una observaciÃ³n activa. Solo se aplican cuando la siguiente
    # observaciÃ³n (RE_OBS) confirma con su prefijo que el cambio es real.
    # Esto evita que listas numeradas ("1. Especie", "21. Especie") dentro
    # de una observaciÃ³n corrompan los tÃ­tulos de secciÃ³n.
    pending_sec1: Optional[Tuple[str, str]] = None  # (prefix, name)
    pending_sec2: Optional[Tuple[str, str]] = None

    for item in items:
        if item["kind"] != "text":
            item["section_1"] = current_sec1_name
            item["section_2"] = current_sec2_name
            item["observation_id"] = current_obs
            continue

        text = item["text"]

        m_obs = RE_OBS.match(text)
        m_sec2 = RE_SEC2.match(text)
        m_sec1 = RE_SEC1.match(text)

        if m_obs:
            if not prefix_matches_section(m_obs.group(1), current_sec1_name, pending_sec1):
                item["section_1"] = current_sec1_name
                item["section_2"] = current_sec2_name
                item["observation_id"] = current_obs
                continue

            # Resolver secciones pendientes antes de iniciar la nueva observaciÃ³n
            (current_sec1_name, current_sec2_name,
             pending_sec1, pending_sec2) = _resolve_pending_sections(
                m_obs.group(1),
                current_sec1_name, current_sec2_name,
                pending_sec1, pending_sec2,
            )

            current_obs = canonical_numbered_id(m_obs.group(1))
            item["section_1"] = current_sec1_name
            item["section_2"] = current_sec2_name
            item["observation_id"] = current_obs
            continue

        if m_sec2 and not is_observation_prefix(m_sec2.group(1)):
            sec2_prefix = m_sec2.group(1).strip()
            sec2_name = m_sec2.group(2).strip()

            if not prefix_matches_section(sec2_prefix, current_sec1_name, pending_sec1):
                item["section_1"] = current_sec1_name
                item["section_2"] = current_sec2_name
                item["observation_id"] = current_obs
                continue

            if is_level2_observation_text(text):
                # Es una observaciÃ³n de nivel 2 â€” resolver pendientes primero
                (current_sec1_name, current_sec2_name,
                 pending_sec1, pending_sec2) = _resolve_pending_sections(
                    sec2_prefix,
                    current_sec1_name, current_sec2_name,
                    pending_sec1, pending_sec2,
                )
                current_obs = canonical_numbered_id(sec2_prefix)
                item["section_1"] = current_sec1_name
                item["section_2"] = current_sec2_name
                item["observation_id"] = current_obs
                continue

            if current_obs is not None:
                # Dentro de una observaciÃ³n: guardar como pendiente
                heading_assigned = False
                if sec2_name and not re.match(r"^\d", sec2_name):
                    current_sec2_name = canonical_section_label(sec2_prefix, sec2_name)
                    current_obs = None
                    pending_sec2 = None
                    heading_assigned = True
                item["section_1"] = current_sec1_name
                item["section_2"] = current_sec2_name
                item["observation_id"] = None if heading_assigned else current_obs
                continue

            # Fuera de observaciÃ³n: aplicar directamente
            if sec2_name and not re.match(r"^\d", sec2_name):
                current_sec2_name = canonical_section_label(sec2_prefix, sec2_name)
                current_obs = None
                pending_sec2 = None

            item["section_1"] = current_sec1_name
            item["section_2"] = current_sec2_name
            item["observation_id"] = current_obs
            continue

        if m_sec1 and prefix_depth(m_sec1.group(1)) == 1:
            sec1_prefix = m_sec1.group(1).strip()
            sec1_name = m_sec1.group(2).strip()

            if current_obs is not None:
                # Dentro de una observaciÃ³n: guardar como pendiente
                if sec1_name and not re.match(r"^\d", sec1_name):
                    pending_sec1 = (sec1_prefix, sec1_name)
                item["section_1"] = current_sec1_name
                item["section_2"] = current_sec2_name
                item["observation_id"] = current_obs
                continue

            # Fuera de observaciÃ³n: aplicar directamente
            if sec1_name and not re.match(r"^\d", sec1_name):
                current_sec1_name = canonical_section_label(sec1_prefix, sec1_name)
                current_sec2_name = None
                current_obs = None
                pending_sec1 = None

            item["section_1"] = current_sec1_name
            item["section_2"] = current_sec2_name
            item["observation_id"] = current_obs
            continue

        item["section_1"] = current_sec1_name
        item["section_2"] = current_sec2_name
        item["observation_id"] = current_obs


def attach_non_text_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    current_sec1 = None
    current_sec2 = None
    current_obs = None

    for item in items:
        if item["kind"] == "text":
            current_sec1 = item.get("section_1")
            current_sec2 = item.get("section_2")
            current_obs = item.get("observation_id")
        else:
            item["section_1"] = current_sec1
            item["section_2"] = current_sec2
            item["observation_id"] = current_obs

    return items


# =========================================================
# SALIDA FINAL
# CAMBIO: las tablas ahora incluyen table_data, table_md,
# y extraction_method en el JSON de salida.
# =========================================================

def build_output(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    obs_map: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for item in items:
        obs_id = item.get("observation_id")
        if not obs_id:
            continue

        if obs_id not in obs_map:
            obs_map[obs_id] = {
                "observation_id": obs_id,
                "section_1": item.get("section_1"),
                "section_2": item.get("section_2"),
                "requirement_types": set(),
                "text_parts": [],
                "tables": [],
                "images": [],
            }
            order.append(obs_id)

        entry = obs_map[obs_id]

        if item["kind"] == "text":
            t = item["text"].strip()
            if RE_TABLA_STRICT.match(t) or RE_FIG.match(t):
                pass
            else:
                entry["text_parts"].append((item["page"], item["bbox"][1], item["text"]))
                entry["requirement_types"].update(detect_requirement_types(item["text"]))

        elif item["kind"] == "table":
            td = item.get("table_data")
            # Extraer rows del table_data; si no hay, fallback a texto plano
            if td and td.get("rows"):
                rows = td["rows"]
            else:
                # Fallback: texto plano como filas de 1 columna
                raw_text = item.get("text", "").strip()
                if raw_text:
                    rows = [[line.strip()] for line in raw_text.split("\n") if line.strip()]
                else:
                    rows = []
            entry["tables"].append(
                {
                    "table_file": item.get("table_file"),
                    "rows": rows,
                }
            )

        elif item["kind"] == "image":
            entry["images"].append(
                {
                    "image_file": item.get("file"),
                    "caption": item.get("caption"),
                }
            )

    output = []
    for obs_id in order:
        entry = obs_map[obs_id]
        entry["text_parts"].sort(key=lambda x: (x[0], x[1]))
        full_text = one_line(" ".join(p[2] for p in entry["text_parts"] if p[2]))

        # --- Limpiar tÃ­tulos de secciÃ³n residuales al final del texto ---
        # En el PDF, tÃ­tulos como "1.2. UbicaciÃ³n" o "7.7. Fauna" aparecen
        # como texto al final del Ãºltimo pÃ¡rrafo de la observaciÃ³n anterior.
        # Se eliminan iterativamente (puede haber mÃ¡s de uno encadenado).
        # Requisitos para detectar un tÃ­tulo residual:
        #   1. Precedido por un carÃ¡cter de fin de oraciÃ³n (. : ) ] ")
        #   2. NÃºmero de secciÃ³n (ej: "1.", "1.2.", "7.7.")
        #   3. Seguido de texto que empieza en mayÃºscula (â‰¥3 chars)
        while True:
            m = re.search(
                r'([.:)\]"])\s+(\d+(?:\.\d+)*\.)\s+([A-ZÃÃ‰ÃÃ“ÃšÃ‘].{2,})\s*$',
                full_text,
            )
            if not m:
                break
            full_text = full_text[: m.start(1) + 1].strip()

        m_topic = RE_TRAILING_TOPIC_HEADING.search(full_text)
        if m_topic:
            full_text = full_text[: m_topic.start(1) + 1].strip()

        output.append(
            {
                "observation_id": entry["observation_id"],
                "section_1": entry["section_1"],
                "section_2": entry["section_2"],
                "requirement_types": sorted(entry["requirement_types"]),
                "text": full_text,
                "tables": entry["tables"],
                "images": entry["images"],
            }
        )

    return output


# =========================================================
# PROCESO PRINCIPAL
# =========================================================

def build_summary(output: List[Dict[str, Any]], tables_document: Dict[str, Any]) -> Dict[str, Any]:
    """
    Genera indicadores y estadÃ­sticas del ICSARA procesado.
    """
    total_obs = len(output)

    # --- DistribuciÃ³n por secciÃ³n 1 ---
    sec1_counts: Dict[str, int] = defaultdict(int)
    for obs in output:
        sec1 = obs.get("section_1") or "(sin secciÃ³n)"
        sec1_counts[sec1] += 1

    # --- DistribuciÃ³n por secciÃ³n 2 ---
    sec2_counts: Dict[str, int] = defaultdict(int)
    for obs in output:
        sec2 = obs.get("section_2")
        if sec2:
            sec2_counts[sec2] += 1

    # --- Ranking de requirement_types ---
    req_counts: Dict[str, int] = defaultdict(int)
    for obs in output:
        for rt in obs.get("requirement_types", []):
            req_counts[rt] += 1
    req_ranking = sorted(req_counts.items(), key=lambda x: x[1], reverse=True)

    # --- Tablas e imÃ¡genes ---
    total_tables = sum(len(obs.get("tables", [])) for obs in output)
    total_images = sum(len(obs.get("images", [])) for obs in output)
    obs_with_tables = sum(1 for obs in output if obs.get("tables"))
    obs_with_images = sum(1 for obs in output if obs.get("images"))
    obs_only_text = sum(
        1 for obs in output
        if not obs.get("tables") and not obs.get("images")
    )

    # --- MÃ©todos de extracciÃ³n de tablas ---
    extraction_methods: Dict[str, int] = defaultdict(int)
    for t in tables_document.get("tables", []):
        m = t.get("extraction_method", "unknown")
        extraction_methods[m] += 1

    # --- Largo promedio de texto ---
    text_lengths = [len(obs.get("text", "")) for obs in output]
    avg_text_len = round(sum(text_lengths) / len(text_lengths), 1) if text_lengths else 0
    max_text_len = max(text_lengths) if text_lengths else 0
    max_text_obs = ""
    if text_lengths:
        max_idx = text_lengths.index(max_text_len)
        max_text_obs = output[max_idx].get("observation_id", "")

    # --- Observaciones por complejidad (cantidad de requirement_types) ---
    complexity = {"baja (1-2)": 0, "media (3-5)": 0, "alta (6+)": 0}
    for obs in output:
        n = len(obs.get("requirement_types", []))
        if n <= 2:
            complexity["baja (1-2)"] += 1
        elif n <= 5:
            complexity["media (3-5)"] += 1
        else:
            complexity["alta (6+)"] += 1

    return {
        "total_observaciones": total_obs,
        "total_tablas": total_tables,
        "total_imagenes": total_images,
        "observaciones_con_tablas": obs_with_tables,
        "observaciones_con_imagenes": obs_with_images,
        "observaciones_solo_texto": obs_only_text,
        "complejidad_observaciones": complexity,
        "largo_promedio_texto_chars": avg_text_len,
        "observacion_mas_larga": {
            "observation_id": max_text_obs,
            "chars": max_text_len,
        },
        "distribucion_seccion_1": dict(
            sorted(sec1_counts.items(), key=lambda x: x[1], reverse=True)
        ),
        "distribucion_seccion_2": dict(
            sorted(sec2_counts.items(), key=lambda x: x[1], reverse=True)
        ),
        "ranking_requirement_types": [
            {"tipo": rt, "frecuencia": count} for rt, count in req_ranking
        ],
        "metodos_extraccion_tablas": dict(extraction_methods),
    }


# =========================================================
# PROCESO PRINCIPAL (con progreso en vivo)
# =========================================================

def _log(msg: str) -> None:
    """Print con flush inmediato para progreso en vivo."""
    # Consolas Windows cp1252 no soportan todos los caracteres; degradar sin fallar.
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        import sys
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc), flush=True)


def process_pdf(pdf_path: str, base_dir: str, artifact_stem: str | None = None) -> Tuple[str, str, str]:
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"No existe el PDF: {pdf_path}")

    stem = artifact_stem or pdf_stem(pdf_path)
    out_dir = base_dir
    images_dir = os.path.join(out_dir, "images")
    tables_dir = os.path.join(out_dir, "tables")
    main_json_path = os.path.join(out_dir, f"{stem}.json")
    tables_json_path = os.path.join(out_dir, "tablas_detectadas.json")
    resumen_json_path = os.path.join(out_dir, "resumen.json")

    ensure_dir(out_dir)
    ensure_dir(images_dir)
    ensure_dir(tables_dir)

    _log(f"â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log(f"  ICSARA PDF Parser v3")
    _log(f"  PDF: {os.path.basename(pdf_path)}")
    _log(f"  Salida: {out_dir}")
    _log(f"â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")

    # --- Fase 1: Abrir PDF ---
    _log(f"\n[1/7] Abriendo PDF...")
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    _log(f"       â†’ {total_pages} pÃ¡ginas detectadas")

    # --- Fase 2: ExtracciÃ³n de texto ---
    _log(f"\n[2/7] Extrayendo texto por pÃ¡gina...")
    page_text_blocks_by_page: Dict[int, List[Dict[str, Any]]] = {}
    total_blocks = 0

    for pno in range(total_pages):
        page = doc[pno]
        page_no = pno + 1
        page_height = float(page.rect.height)

        lines = extract_lines(page)
        text_blocks = group_lines(lines)
        text_blocks = merge_adjacent_blocks(text_blocks)
        text_blocks = [
            b for b in text_blocks
            if not (
                is_footer_or_noise(b.get("text", ""))
                or is_footer_zone(b.get("bbox", (0, 0, 0, 0)), page_height, threshold=0.9)
            )
        ]

        for b in text_blocks:
            b["page"] = page_no

        page_text_blocks_by_page[page_no] = text_blocks
        total_blocks += len(text_blocks)

        if page_no % 10 == 0 or page_no == total_pages:
            _log(f"       â†’ PÃ¡gina {page_no}/{total_pages} ({total_blocks} bloques acumulados)")

    _log(f"       âœ“ {total_blocks} bloques de texto extraÃ­dos")

    # --- Fase 3: DetecciÃ³n y extracciÃ³n de tablas ---
    _log(f"\n[3/7] Detectando y extrayendo tablas...")
    tables_by_page, tables_document = extract_tables(
        doc=doc,
        tables_dir=tables_dir,
        page_text_blocks_by_page=page_text_blocks_by_page,
        pdf_path=pdf_path,
    )
    total_tables = tables_document.get("table_count", 0)
    _log(f"       âœ“ {total_tables} tablas detectadas")

    # Mostrar mÃ©todos de extracciÃ³n
    method_counts: Dict[str, int] = defaultdict(int)
    for t in tables_document.get("tables", []):
        method_counts[t.get("extraction_method", "unknown")] += 1
    for method, count in sorted(method_counts.items()):
        _log(f"         Â· {method}: {count}")

    # --- Fase 4: ExtracciÃ³n de imÃ¡genes ---
    _log(f"\n[4/7] Extrayendo imÃ¡genes...")
    all_items: List[Dict[str, Any]] = []
    total_images = 0

    for pno in range(total_pages):
        page = doc[pno]
        page_no = pno + 1

        text_blocks = page_text_blocks_by_page.get(page_no, [])
        table_items = tables_by_page.get(page_no, [])
        image_items = extract_images(page, images_dir)

        for im in image_items:
            im["page"] = page_no

        total_images += len(image_items)

        page_items = []
        page_items.extend(text_blocks)
        page_items.extend(table_items)
        page_items.extend(image_items)
        page_items.sort(key=lambda x: (x["page"], round(x["bbox"][1], 1), round(x["bbox"][0], 1)))

        attach_figure_captions(page_items)
        all_items.extend(page_items)

    _log(f"       âœ“ {total_images} imÃ¡genes extraÃ­das")

    # --- Fase 5: Filtrado y asignaciÃ³n de secciones ---
    _log(f"\n[5/7] Asignando secciones y observaciones...")
    filtered_items = []
    for item in all_items:
        if item["kind"] == "text" and RE_FIG.match(item["text"]):
            continue
        filtered_items.append(item)

    filtered_items.sort(key=lambda x: (x["page"], round(x["bbox"][1], 1), round(x["bbox"][0], 1)))
    filtered_items = remove_table_text_blocks(filtered_items)
    filtered_items = split_embedded_observation_blocks(filtered_items)

    detect_sections_and_observations(filtered_items)
    filtered_items = attach_non_text_items(filtered_items)
    _log(f"       âœ“ {len(filtered_items)} Ã­tems procesados")

    # --- Fase 6: ConstrucciÃ³n del JSON de salida ---
    _log(f"\n[6/7] Construyendo JSON de observaciones...")
    output = build_output(filtered_items)
    _log(f"       âœ“ {len(output)} observaciones sistematizadas")

    # Conteo rÃ¡pido
    obs_with_t = sum(1 for o in output if o.get("tables"))
    obs_with_i = sum(1 for o in output if o.get("images"))
    _log(f"         Â· Con tablas: {obs_with_t}")
    _log(f"         Â· Con imÃ¡genes: {obs_with_i}")
    _log(f"         Â· Solo texto: {len(output) - obs_with_t - obs_with_i + sum(1 for o in output if o.get('tables') and o.get('images'))}")

    # --- Fase 7: Generar resumen e indicadores ---
    _log(f"\n[7/7] Generando resumen e indicadores...")
    summary = build_summary(output, tables_document)

    # Guardar archivos
    with open(main_json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with open(tables_json_path, "w", encoding="utf-8") as f:
        json.dump(tables_document, f, ensure_ascii=False, indent=2)

    with open(resumen_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    doc.close()

    # --- Resumen final en pantalla ---
    _log(f"\nâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log(f"  RESULTADO")
    _log(f"â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log(f"  Observaciones: {summary['total_observaciones']}")
    _log(f"  Tablas:        {summary['total_tablas']}")
    _log(f"  ImÃ¡genes:      {summary['total_imagenes']}")
    _log(f"  Complejidad:   {summary['complejidad_observaciones']}")
    _log(f"")
    _log(f"  Top 5 requirement_types:")
    for item in summary["ranking_requirement_types"][:5]:
        _log(f"    Â· {item['tipo']}: {item['frecuencia']}")
    _log(f"")
    _log(f"  Archivos generados:")
    _log(f"    Â· {main_json_path}")
    _log(f"    Â· {tables_json_path}")
    _log(f"    Â· {resumen_json_path}")
    _log(f"â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")

    return main_json_path, tables_json_path, resumen_json_path


def run_extraction(
    pdf_path: Path | str,
    out_dir: Path | str,
    include_png: bool = True,
    artifact_stem: str | None = None,
) -> ExtractionSummary:
    """Motor de extracción del pipeline.

    Por defecto usa la segmentación con IA (app.pipeline.segment_ia, port de
    segmentador_ia_icsara v2.0: la IA señala límites, el código ensambla
    textual). Control por variables de entorno:
      ICSARA_EXTRACTION_ENGINE = "ia" (default) | "heuristic"
      ICSARA_IA_FALLBACK_HEURISTIC = "true" (default) | "false"
    Con fallback activo, cualquier fallo de la vía IA (sin API key, error de
    etiquetado, etc.) se registra y el job continúa con el parser heurístico.
    """
    engine = os.getenv("ICSARA_EXTRACTION_ENGINE", "ia").strip().lower()
    if engine != "heuristic":
        try:
            from app.pipeline.segment_ia import run_extraction_ia

            return run_extraction_ia(
                pdf_path=pdf_path,
                out_dir=out_dir,
                artifact_stem=artifact_stem,
            )
        except Exception:
            fallback = os.getenv("ICSARA_IA_FALLBACK_HEURISTIC", "true").strip().lower()
            if fallback in ("0", "false", "no"):
                raise
            import logging
            logging.getLogger(__name__).exception(
                "Segmentación IA falló; usando parser heurístico como respaldo."
            )

    return _run_extraction_heuristic(
        pdf_path=pdf_path,
        out_dir=out_dir,
        include_png=include_png,
        artifact_stem=artifact_stem,
    )


def _run_extraction_heuristic(
    pdf_path: Path | str,
    out_dir: Path | str,
    include_png: bool = True,
    artifact_stem: str | None = None,
) -> ExtractionSummary:
    del include_png  # The validated parser always exports its media assets.

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    main_json, tables_json, resumen_json = process_pdf(
        str(pdf_path),
        str(out_dir),
        artifact_stem=artifact_stem,
    )

    with open(resumen_json, "r", encoding="utf-8") as handle:
        resumen = json.load(handle)

    with fitz.open(str(pdf_path)) as doc:
        pages = len(doc)

    return ExtractionSummary(
        pages=pages,
        observaciones=int(resumen.get("total_observaciones", 0)),
        tablas=int(resumen.get("total_tablas", 0)),
        imagenes=int(resumen.get("total_imagenes", 0)),
        output_dir=out_dir,
        output_json=Path(main_json),
        tables_json=Path(tables_json),
        summary_json=Path(resumen_json),
        images_dir=out_dir / "images",
        tables_dir=out_dir / "tables",
    )


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":
    try:
        main_json, tables_json, resumen_json = process_pdf(PDF_PATH, BASE_DIR)
    except Exception as e:
        print(f"ERROR: {e}")

