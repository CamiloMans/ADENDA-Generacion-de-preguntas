# -*- coding: utf-8 -*-
"""
segment_ia.py — Segmentación de ICSARA con IA (port de segmentador_ia_icsara v2.0)
==================================================================================
La IA SEÑALA límites de segmentos pero NUNCA escribe texto. El código ensambla
textual y verifica que cada observación sea reconstruible exactamente desde el PDF.

Capacidades portadas del pipeline heurístico original:
  · Extracción estructurada de tablas en cascada:
      grilla heurística (líneas vectoriales) → PyMuPDF find_tables →
      pdfplumber → OCR Tesseract sobre el screenshot.
  · Screenshot PNG de cada tabla (220 dpi) y Markdown de la tabla.
  · Captions de tablas ("Tabla N° ...") por proximidad.
  · Extracción de figuras/imágenes con filtros de ruido, PNG y captions.
  · Fusión de tablas partidas por salto de página.

Salidas (compatibles con el resto del pipeline: classify → review):
  {stem}.json               observaciones (mismo esquema del parser heurístico)
  tablas_detectadas.json    documento de tablas
  resumen.json              estadísticas build_summary
  {stem}_lineas.json        índice de líneas extraídas
  {stem}_segmentos.json     etiquetas devueltas por la IA
  {stem}_verificacion.json  reporte de fidelidad y cobertura

Punto de entrada: run_extraction_ia(pdf_path, out_dir, artifact_stem=...) -> ExtractionSummary
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF

from app.pipeline.types import ExtractionSummary

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    from PIL import Image
    import pytesseract
    TESSERACT_AVAILABLE = True
except Exception:
    TESSERACT_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
PAGINAS_POR_LOTE = 4
DPI_IMAGEN_IA = 120       # render de páginas para la IA
DPI_TABLA = 220           # screenshot de tablas
MAX_TOKENS = 8000

TIPOS_VALIDOS = {"seccion_1", "seccion_2", "observacion", "tabla", "figura",
                 "contexto", "ruido"}

RE_TABLA = re.compile(r"^\s*Tabla(?:\s+N[°º]?\s*\d+)?", re.IGNORECASE)
RE_FIG = re.compile(r"^\s*Figura\s+N[°º]?\s*\d+", re.IGNORECASE)

# Filtros de ruido para imágenes (idénticos al original)
MIN_IMAGE_WIDTH = 60.0
MIN_IMAGE_HEIGHT = 60.0
MIN_IMAGE_AREA = 5000.0
IMAGE_DUP_IOU = 0.65
IMAGE_MERGE_GAP = 6.0
IMAGE_BBOX_PAD = 3.0


def _log(msg: str) -> None:
    # Consolas Windows cp1252 no soportan todos los caracteres; degradar sin fallar.
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc), flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# UTILIDADES GEOMÉTRICAS (portadas del original)
# ═══════════════════════════════════════════════════════════════════════════

def rect_area(r: fitz.Rect) -> float:
    return max(0.0, float(r.width)) * max(0.0, float(r.height))


def union_rect(a: fitz.Rect, b: fitz.Rect) -> fitz.Rect:
    return fitz.Rect(min(a.x0, b.x0), min(a.y0, b.y0),
                     max(a.x1, b.x1), max(a.y1, b.y1))


def iou_rect(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = a & b
    ia = 0.0 if inter.is_empty else rect_area(inter)
    if ia <= 0:
        return 0.0
    u = rect_area(a) + rect_area(b) - ia
    return ia / u if u > 0 else 0.0


def rects_touch_or_overlap(a: fitz.Rect, b: fitz.Rect, tol: float = 0.0) -> bool:
    aa = fitz.Rect(a.x0 - tol, a.y0 - tol, a.x1 + tol, a.y1 + tol)
    bb = fitz.Rect(b.x0 - tol, b.y0 - tol, b.x1 + tol, b.y1 + tol)
    return not (aa & bb).is_empty


def merge_rects(rects: List[fitz.Rect], gap: float = 5.0) -> List[fitz.Rect]:
    pending, merged = rects[:], []
    while pending:
        cur = pending.pop(0)
        changed = True
        while changed:
            changed, keep = False, []
            for r in pending:
                if rects_touch_or_overlap(cur, r, tol=gap):
                    cur = union_rect(cur, r); changed = True
                else:
                    keep.append(r)
            pending = keep
        merged.append(cur)
    return merged


def deduplicate_rects(rects: List[fitz.Rect], iou_thr: float = 0.65) -> List[fitz.Rect]:
    out: List[fitz.Rect] = []
    for r in sorted(rects, key=rect_area, reverse=True):
        if not any(iou_rect(r, k) >= iou_thr for k in out):
            out.append(r)
    return sorted(out, key=lambda z: (z.y0, z.x0))


def expand_rect(rect: fitz.Rect, pad: float, page_rect: fitz.Rect) -> fitz.Rect:
    return fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad) & page_rect


def is_inside_header_footer(rect: fitz.Rect, page_rect: fitz.Rect,
                            header_ratio: float = 0.08, footer_ratio: float = 0.08) -> bool:
    cy = (rect.y0 + rect.y1) / 2.0
    return (cy <= page_rect.y0 + page_rect.height * header_ratio or
            cy >= page_rect.y1 - page_rect.height * footer_ratio)


def rect_to_dict(rect: fitz.Rect) -> Dict[str, float]:
    return {"x0": round(rect.x0, 3), "y0": round(rect.y0, 3),
            "x1": round(rect.x1, 3), "y1": round(rect.y1, 3),
            "width": round(rect.width, 3), "height": round(rect.height, 3),
            "area": round(rect_area(rect), 3)}


def save_bbox_screenshot(doc, page_index0: int, bbox: fitz.Rect,
                         out_dir: str, fname: str, dpi: int = DPI_TABLA) -> Optional[str]:
    try:
        page = doc[page_index0]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                              clip=bbox, alpha=False)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, fname)
        pix.save(path)
        return path
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# FASE 1 · EXTRACCIÓN DETERMINISTA DE LÍNEAS
# ═══════════════════════════════════════════════════════════════════════════

def extraer_lineas(doc: fitz.Document) -> List[Dict[str, Any]]:
    """Todas las líneas de texto, en orden de lectura, con ID global,
    página y bbox. Texto EXACTO (solo \\xa0 → espacio)."""
    lineas: List[Dict[str, Any]] = []
    for pno in range(doc.page_count):
        raw = doc[pno].get_text("dict")
        page_lines = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for ln in block.get("lines", []):
                text = "".join(s.get("text", "") for s in ln.get("spans", []))
                text = text.replace("\xa0", " ")
                if not text.strip():
                    continue
                page_lines.append({"page": pno + 1,
                                   "bbox": [round(v, 2) for v in ln["bbox"]],
                                   "y": ln["bbox"][1], "x": ln["bbox"][0],
                                   "text": text})
        page_lines.sort(key=lambda l: (round(l["y"], 1), l["x"]))
        lineas.extend(page_lines)
    for i, l in enumerate(lineas):
        l["id"] = f"L{i:05d}"
        l.pop("y", None); l.pop("x", None)
    return lineas


def render_pagina_b64(doc, pno1: int, dpi: int = DPI_IMAGEN_IA) -> str:
    pix = doc[pno1 - 1].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    return base64.standard_b64encode(pix.tobytes("png")).decode()


# ═══════════════════════════════════════════════════════════════════════════
# FASE 2 · ETIQUETADO POR IA
# ═══════════════════════════════════════════════════════════════════════════

PROMPT_SISTEMA = """Eres un experto en documentos ICSARA del SEA de Chile
(Informe Consolidado de Solicitud de Aclaraciones, Rectificaciones y/o
Ampliaciones). Tu única tarea es SEGMENTAR: decidir dónde empieza y termina
cada elemento. NUNCA transcribes ni corriges texto.

Recibirás páginas del PDF como imagen junto con sus líneas de texto
numeradas con IDs (L00000, L00001, ...). Debes devolver EXCLUSIVAMENTE un
JSON con esta forma:

{"segmentos": [
  {"tipo": "seccion_1",   "id": "3.",     "desde": "L00010", "hasta": "L00010"},
  {"tipo": "seccion_2",   "id": "3.1.",   "desde": "L00011", "hasta": "L00011"},
  {"tipo": "observacion", "id": "3.1.2.", "desde": "L00012", "hasta": "L00019"},
  {"tipo": "tabla",       "id": null,     "desde": "L00020", "hasta": "L00034"},
  {"tipo": "ruido",       "id": null,     "desde": "L00035", "hasta": "L00036"}
]}

Reglas estrictas:
1. tipo ∈ {seccion_1, seccion_2, observacion, tabla, figura, contexto, ruido}.
   - seccion_1 / seccion_2: títulos de capítulo y subcapítulo.
   - observacion: una pregunta/requerimiento de la autoridad, completa,
     incluyendo todos sus párrafos, listas y viñetas.
   - tabla / figura: contenido tabular o pies de figura. Si una tabla o
     figura está DENTRO de una observación, etiquétala como tabla/figura
     (el sistema la asociará a la observación abierta).
   - contexto: texto informativo que NO es una pregunta/requerimiento a
     responder (portada, instrucciones generales, preámbulos de sección,
     observaciones citadas que el propio documento declara no consideradas).
   - ruido: encabezados y pies de página, folios, firmas electrónicas,
     URLs de validación, numeración de página.
2. "id" es la numeración LITERAL tal como aparece impresa (ej: "3.1.2.").
   Los formatos varían según la región y el redactor: puede haber 2, 3 o 4
   niveles, formatos como "Observación N° 5", letras, etc. Usa la imagen
   para decidir la jerarquía real (tamaño de fuente, negrita, sangría).
   Para observaciones sin numeración visible usa un id secuencial "OBS-n".
3. Cobertura total: CADA línea listada debe quedar dentro de exactamente un
   segmento. Los segmentos no se solapan y van en orden.
4. Usa SOLO los IDs de línea entregados. No inventes IDs.
5. Si el lote empieza a mitad de un elemento abierto en páginas anteriores
   (se te indicará), etiqueta esas primeras líneas con el mismo tipo e id
   del elemento abierto.
6. Una observación NO termina por un salto de página: termina cuando
   empieza otra observación, otro título, o el documento cambia de materia.
7. Usa seccion_2 SOLO para títulos o preámbulos que introducen una lista de
   observaciones numeradas (ej: "3.2 Respecto de los antecedentes... se
   señala:" seguido de 3.2.1, 3.2.2...). Si un número de subsección (ej:
   "3.1") va seguido de texto sustantivo dirigido al titular (un
   pronunciamiento, aclaración o requerimiento) y NO tiene
   sub-observaciones numeradas debajo, etiqueta TODO ese bloque como
   observacion con id "3.1." — no como seccion_2 ni contexto.
8. Responde SOLO el JSON, sin comentarios ni markdown."""


def _construir_mensaje_lote(doc, lineas_lote, paginas, estado_abierto):
    contenido = []
    if estado_abierto:
        contenido.append({"type": "text", "text":
            f"ESTADO: viene abierto un segmento tipo '{estado_abierto['tipo']}' "
            f"id '{estado_abierto.get('id')}' desde páginas anteriores. "
            f"Si las primeras líneas le pertenecen, etiquétalas con ese mismo tipo e id."})
    for pno in paginas:
        contenido.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": render_pagina_b64(doc, pno)}})
        listado = "\n".join(f"[{l['id']}] {l['text']}" for l in lineas_lote if l["page"] == pno)
        contenido.append({"type": "text", "text": f"— Página {pno} — líneas:\n{listado}"})
    contenido.append({"type": "text", "text":
        "Segmenta TODAS las líneas anteriores. Responde solo el JSON."})
    return contenido


def _parsear_json(texto: str) -> Dict[str, Any]:
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if not m:
        raise ValueError("La respuesta no contiene JSON")
    return json.loads(m.group(0))


def _validar_segmentos(segs: List[Dict], lineas_lote: List[Dict]) -> List[str]:
    errores = []
    ids_lote = [l["id"] for l in lineas_lote]
    idx = {lid: i for i, lid in enumerate(ids_lote)}
    cubiertas, ultimo = set(), -1
    for s in segs:
        if s.get("tipo") not in TIPOS_VALIDOS:
            errores.append(f"tipo inválido: {s.get('tipo')}"); continue
        d, h = s.get("desde"), s.get("hasta")
        if d not in idx or h not in idx:
            errores.append(f"ID de línea inexistente en {d}..{h}"); continue
        i0, i1 = idx[d], idx[h]
        if i1 < i0:
            errores.append(f"rango invertido {d}..{h}"); continue
        if i0 <= ultimo:
            errores.append(f"solape o desorden en {d}..{h}")
        ultimo = max(ultimo, i1)
        cubiertas.update(ids_lote[i0:i1 + 1])
    faltantes = [lid for lid in ids_lote if lid not in cubiertas]
    if faltantes:
        errores.append(f"{len(faltantes)} líneas sin asignar (p.ej. {faltantes[:5]})")
    return errores


def _ultima_obs_id(segmentos: List[Dict]) -> Optional[str]:
    for s in reversed(segmentos):
        if s["tipo"] == "observacion":
            return s.get("id")
    return None


def etiquetar_con_ia(doc, lineas, api_key: str, model: str,
                     paginas_por_lote: int = PAGINAS_POR_LOTE) -> List[Dict]:
    import anthropic
    if not api_key:
        raise RuntimeError(
            "Segmentación IA requiere una API key de Anthropic "
            "(ANTHROPIC_ADENDA_VALIDACION_ICSARA_API_KEY o ANTHROPIC_API_KEY)."
        )
    client = anthropic.Anthropic(api_key=api_key)

    todas_paginas = sorted({l["page"] for l in lineas})
    segmentos: List[Dict] = []
    estado_abierto: Optional[Dict] = None

    for i in range(0, len(todas_paginas), paginas_por_lote):
        paginas = todas_paginas[i:i + paginas_por_lote]
        lote = [l for l in lineas if l["page"] in paginas]
        if not lote:
            continue
        _log(f"  · IA etiquetando páginas {paginas[0]}–{paginas[-1]} ({len(lote)} líneas)")

        contenido = _construir_mensaje_lote(doc, lote, paginas, estado_abierto)
        errores_previos: List[str] = []
        segs: List[Dict] = []
        for _ in range(2):
            extra = ([{"type": "text", "text":
                       "Tu respuesta anterior tenía errores: " + "; ".join(errores_previos) +
                       ". Corrígelos y responde de nuevo solo el JSON."}]
                     if errores_previos else [])
            resp = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=PROMPT_SISTEMA,
                messages=[{"role": "user", "content": contenido + extra}])
            try:
                segs = _parsear_json(resp.content[0].text).get("segmentos", [])
                errores_previos = _validar_segmentos(segs, lote)
            except Exception as e:
                errores_previos = [str(e)]
            if not errores_previos:
                break
        if errores_previos:
            raise RuntimeError(f"Etiquetado inválido en páginas {paginas}: {errores_previos}")

        segmentos.extend(segs)
        estado_abierto = None
        for s in reversed(segs):
            if s["tipo"] == "observacion":
                estado_abierto = {"tipo": "observacion", "id": s.get("id")}; break
            if s["tipo"] in ("tabla", "figura"):
                oid = _ultima_obs_id(segmentos)
                if oid:
                    estado_abierto = {"tipo": "observacion", "id": oid}
                break
            if s["tipo"] in ("seccion_1", "seccion_2", "contexto"):
                break
    return segmentos


# ═══════════════════════════════════════════════════════════════════════════
# TABLAS · EXTRACCIÓN ESTRUCTURADA EN CASCADA (portada del original)
# ═══════════════════════════════════════════════════════════════════════════

def _heuristic_extract_table(page: fitz.Page, rect: fitz.Rect) -> Optional[Dict]:
    """Grilla desde líneas vectoriales H/V + asignación de spans a celdas."""
    h_ys: List[float] = []
    v_xs: List[float] = []
    pad = 3.0
    clip = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)

    for d in page.get_drawings():
        for it in d.get("items", []):
            op = it[0]
            if op == "l":
                p1, p2 = it[1], it[2]
                x1f, y1f, x2f, y2f = float(p1.x), float(p1.y), float(p2.x), float(p2.y)
                mid = fitz.Rect(min(x1f, x2f), min(y1f, y2f), max(x1f, x2f), max(y1f, y2f))
                if not rects_touch_or_overlap(mid, clip, tol=2):
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
                    h_ys.extend([r.y0, r.y1]); v_xs.extend([r.x0, r.x1])

    h_ys.extend([rect.y0, rect.y1])
    v_xs.extend([rect.x0, rect.x1])

    def dedup(coords: List[float], tol: float = 4.0) -> List[float]:
        if not coords:
            return []
        coords = sorted(set(coords))
        out = [coords[0]]
        for c in coords[1:]:
            if c - out[-1] > tol:
                out.append(c)
            else:
                out[-1] = (out[-1] + c) / 2.0
        return out

    h_ys, v_xs = dedup(h_ys), dedup(v_xs)
    if len(h_ys) < 2 or len(v_xs) < 2:
        return None
    nr, nc = len(h_ys) - 1, len(v_xs) - 1
    if nr < 1 or nc < 1:
        return None

    grid: List[List[List[str]]] = [[[] for _ in range(nc)] for _ in range(nr)]
    raw = page.get_text("dict", clip=rect)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text", "") or "").strip()
                if not txt:
                    continue
                cx = (span["bbox"][0] + span["bbox"][2]) / 2.0
                cy = (span["bbox"][1] + span["bbox"][3]) / 2.0
                ri = next((i for i in range(nr) if h_ys[i] - 2 <= cy <= h_ys[i + 1] + 2),
                          min(range(nr), key=lambda i: abs(cy - (h_ys[i] + h_ys[i + 1]) / 2)))
                ci = next((i for i in range(nc) if v_xs[i] - 2 <= cx <= v_xs[i + 1] + 2),
                          min(range(nc), key=lambda i: abs(cx - (v_xs[i] + v_xs[i + 1]) / 2)))
                grid[ri][ci].append(txt)

    rows = []
    for ri in range(nr):
        row = []
        for ci in range(nc):
            cell = re.sub(r"\s{2,}", " ", " ".join(grid[ri][ci])).strip()
            row.append(cell if cell else None)
        rows.append(row)
    if not any(any(c is not None for c in r) for r in rows):
        return None
    return {"rows": rows, "num_rows": nr, "num_cols": nc, "method": "heuristic_grid"}


def _pymupdf_extract_table(page: fitz.Page, rect: fitz.Rect) -> Optional[Dict]:
    finder = getattr(page, "find_tables", None)
    if not callable(finder):
        return None
    try:
        res = finder()
    except Exception:
        return None
    best, best_iou = None, 0.0
    for t in getattr(res, "tables", None) or []:
        bbox = getattr(t, "bbox", None)
        if bbox is None:
            continue
        score = iou_rect(rect, fitz.Rect(bbox))
        if score > best_iou:
            best_iou, best = score, t
    if best is None or best_iou < 0.25:
        return None
    try:
        raw_rows = best.extract()
    except Exception:
        return None
    if not raw_rows:
        return None
    rows, max_cols = [], 0
    for row in raw_rows:
        cr = [(" ".join(str(c).split()) or None) if c is not None else None for c in row]
        cr = [c if c else None for c in cr]
        rows.append(cr); max_cols = max(max_cols, len(cr))
    return {"rows": rows, "num_rows": len(rows), "num_cols": max_cols,
            "method": "pymupdf_find_tables"}


def _pdfplumber_extract_table(pdf_path: str, page_index0: int,
                              rect: fitz.Rect) -> Optional[Dict]:
    if not PDFPLUMBER_AVAILABLE:
        return None
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_index0 >= len(pdf.pages):
                return None
            page = pdf.pages[page_index0]
            crop = page.crop((float(rect.x0), float(rect.y0),
                              float(rect.x1), float(rect.y1)))
            settings = {"vertical_strategy": "lines", "horizontal_strategy": "lines",
                        "snap_tolerance": 5, "join_tolerance": 5,
                        "edge_min_length": 15, "min_words_vertical": 1,
                        "min_words_horizontal": 1, "intersection_tolerance": 8}
            tables = crop.extract_tables(settings)
            if not tables:
                settings["vertical_strategy"] = settings["horizontal_strategy"] = "lines_strict"
                tables = crop.extract_tables(settings)
            if not tables:
                settings["vertical_strategy"] = "text"
                settings["horizontal_strategy"] = "lines"
                tables = crop.extract_tables(settings)
            if not tables:
                return None
            best = max(tables, key=lambda t: len(t) * len(t[0]) if t and t[0] else 0)
            if not best:
                return None
            rows, max_cols = [], 0
            for row in best:
                cr = []
                for cell in row:
                    if cell is None or str(cell).strip() == "":
                        cr.append(None)
                    else:
                        cr.append(" ".join(str(cell).split()) or None)
                rows.append(cr); max_cols = max(max_cols, len(cr))
            if max_cols == 0:
                return None
            return {"rows": rows, "num_rows": len(rows), "num_cols": max_cols,
                    "method": "pdfplumber"}
    except Exception:
        return None


# ---- OCR (método 4) --------------------------------------------------------

def _safe_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def ocr_words_from_image(image_path: str) -> List[Dict[str, Any]]:
    if not TESSERACT_AVAILABLE:
        return []
    try:
        img = Image.open(image_path)
        data = pytesseract.image_to_data(img, lang="spa+eng",
                                         output_type=pytesseract.Output.DICT,
                                         config="--psm 6")
    except Exception:
        return []
    words = []
    for i in range(len(data.get("text", []))):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data.get("conf", [-1])[i])
        except Exception:
            conf = -1.0
        if not txt or conf < 0:
            continue
        x, y = _safe_int(data["left"][i]), _safe_int(data["top"][i])
        w, h = _safe_int(data["width"][i]), _safe_int(data["height"][i])
        words.append({"text": txt, "x0": x, "y0": y, "x1": x + w, "y1": y + h})
    return words


def group_words_into_rows(words, y_tol: int = 12):
    if not words:
        return []
    words = sorted(words, key=lambda w: (w["y0"], w["x0"]))
    rows: List[List[Dict]] = []
    for w in words:
        cy = (w["y0"] + w["y1"]) / 2.0
        for row in rows:
            centers = [(r["y0"] + r["y1"]) / 2.0 for r in row]
            if abs(cy - sum(centers) / len(centers)) <= y_tol:
                row.append(w); break
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    rows.sort(key=lambda row: min(w["y0"] for w in row))
    return rows


def infer_column_gaps(rows, min_gap: int = 25) -> List[int]:
    gaps = []
    for row in rows:
        for i in range(1, len(row)):
            g = row[i]["x0"] - row[i - 1]["x1"]
            if g >= min_gap:
                gaps.append(int((row[i]["x0"] + row[i - 1]["x1"]) / 2.0))
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


def _ocr_extract_table(image_path: Optional[str]) -> Optional[Dict]:
    if not image_path or not os.path.isfile(image_path) or not TESSERACT_AVAILABLE:
        return None
    words = ocr_words_from_image(image_path)
    if not words:
        return None
    rows_words = group_words_into_rows(words)
    if not rows_words:
        return None
    splits = infer_column_gaps(rows_words)

    def col_of(w):
        cx = (w["x0"] + w["x1"]) / 2.0
        return sum(1 for s in splits if cx > s)

    result_rows, max_cols = [], 0
    for row in rows_words:
        cols: Dict[int, List[str]] = defaultdict(list)
        for w in row:
            cols[col_of(w)].append(w["text"])
        mc = max(cols.keys()) if cols else -1
        cells: List[Optional[str]] = []
        for c in range(mc + 1):
            t = re.sub(r"\s{2,}", " ", " ".join(cols.get(c, []))).strip()
            cells.append(t if t else None)
        while cells and cells[-1] is None:
            cells.pop()
        if cells:
            result_rows.append(cells); max_cols = max(max_cols, len(cells))
    if not result_rows or max_cols == 0:
        return None
    for r in result_rows:
        while len(r) < max_cols:
            r.append(None)
    if not any(any(c is not None for c in r) for r in result_rows):
        return None
    return {"rows": result_rows, "num_rows": len(result_rows),
            "num_cols": max_cols, "method": "ocr_screenshot"}


# ---- Post-proceso y cascada ------------------------------------------------

def simplify_table_data(td: Optional[Dict]) -> Optional[Dict]:
    """1) Fusiona filas de continuación (col 0 == None). 2) Elimina columnas
    totalmente nulas. 3) Descarta tablas 1x1."""
    if not td or not td.get("rows"):
        return td
    rows, num_cols = td["rows"], td.get("num_cols", 0)
    if num_cols == 0:
        return td
    merged: List[List[Optional[str]]] = []
    for row in rows:
        padded = list(row) + [None] * (num_cols - len(row))
        if merged and padded[0] is None:
            prev = merged[-1]
            for c in range(num_cols):
                if padded[c] is not None:
                    prev[c] = (prev[c].rstrip() + " " + padded[c].lstrip()) \
                        if prev[c] is not None else padded[c]
        else:
            merged.append(list(padded))
    keep = [c for c in range(num_cols) if any(r[c] is not None for r in merged)]
    final = [[r[c] for c in keep] for r in merged]
    res = {"rows": final, "num_rows": len(final), "num_cols": len(keep),
           "method": td.get("method", "unknown")}
    if res["num_rows"] <= 1 and res["num_cols"] <= 1:
        return None
    return res


def extract_table_structured(page, rect, pdf_path, page_index0,
                             image_path=None) -> Optional[Dict]:
    """Cascada: heurístico → PyMuPDF → pdfplumber → OCR."""
    for fn in (lambda: _heuristic_extract_table(page, rect),
               lambda: _pymupdf_extract_table(page, rect),
               lambda: _pdfplumber_extract_table(pdf_path, page_index0, rect)):
        res = fn()
        if res:
            simp = simplify_table_data(res)
            if simp and simp["num_rows"] >= 2:
                return simp
    res = _ocr_extract_table(image_path)
    if res:
        return simplify_table_data(res)
    return None


def table_data_to_markdown(td: Optional[Dict]) -> Optional[str]:
    if not td or not td.get("rows"):
        return None
    num_cols = td.get("num_cols", 0)
    if num_cols == 0:
        return None
    lines = []
    for i, row in enumerate(td["rows"]):
        padded = list(row) + [None] * (num_cols - len(row))
        lines.append("| " + " | ".join(str(c) if c is not None else "" for c in padded) + " |")
        if i == 0:
            lines.append("|" + "|".join(["---"] * num_cols) + "|")
    return "\n".join(lines)


def _fallback_text_to_markdown(vectorial_text: str) -> Optional[str]:
    if not vectorial_text or not vectorial_text.strip():
        return None
    lines = [l.strip() for l in vectorial_text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    md = []
    for i, line in enumerate(lines):
        md.append(f"| {line} |")
        if i == 0:
            md.append("|---|")
    return "\n".join(md)


def find_caption(lineas_pagina: List[Dict], rect: fitz.Rect, patron) -> Optional[str]:
    """Caption por proximidad vertical + solape horizontal (portado)."""
    best, best_score = None, 1e9
    for l in lineas_pagina:
        t = l["text"].strip()
        if not patron.match(t):
            continue
        lr = fitz.Rect(l["bbox"])
        overlap = max(0.0, min(rect.x1, lr.x1) - max(rect.x0, lr.x0))
        if overlap / max(1.0, min(rect.width, lr.width)) < 0.15:
            continue
        if lr.y0 >= rect.y1:
            dist, bias = lr.y0 - rect.y1, 20
        elif rect.y0 >= lr.y1:
            dist, bias = rect.y0 - lr.y1, 0
        else:
            dist, bias = 0, 30
        if dist > 120:
            continue
        if dist + bias < best_score:
            best, best_score = t, dist + bias
    return best


# ═══════════════════════════════════════════════════════════════════════════
# FIGURAS / IMÁGENES (portado del original)
# ═══════════════════════════════════════════════════════════════════════════

def image_is_probably_noise(rect: fitz.Rect, page_rect: fitz.Rect) -> bool:
    return (is_inside_header_footer(rect, page_rect) or
            rect.width < MIN_IMAGE_WIDTH or rect.height < MIN_IMAGE_HEIGHT or
            rect_area(rect) < MIN_IMAGE_AREA)


def collect_image_candidates(page: fitz.Page) -> List[fitz.Rect]:
    raw = page.get_text("dict")
    cands = []
    for block in raw.get("blocks", []):
        if block.get("type") != 1 or not block.get("bbox"):
            continue
        r = fitz.Rect(block["bbox"])
        if image_is_probably_noise(r, page.rect):
            continue
        cands.append(expand_rect(r, IMAGE_BBOX_PAD, page.rect))
    if not cands:
        return []
    return deduplicate_rects(merge_rects(cands, gap=IMAGE_MERGE_GAP),
                             iou_thr=IMAGE_DUP_IOU)


def extraer_imagenes_doc(doc, images_dir: str,
                         lineas_por_pagina: Dict[int, List[Dict]]) -> Dict[int, List[Dict]]:
    """Extrae imágenes por página con PNG y caption 'Figura N°...'."""
    os.makedirs(images_dir, exist_ok=True)
    por_pagina: Dict[int, List[Dict]] = defaultdict(list)
    for pno in range(doc.page_count):
        page = doc[pno]
        page_no = pno + 1
        for idx, rect in enumerate(collect_image_candidates(page), start=1):
            fname = f"page_{page_no:03d}_image_{idx:03d}.png"
            fpath = os.path.join(images_dir, fname)
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
                pix.save(fpath)
            except Exception:
                fpath = None
            caption = find_caption(lineas_por_pagina.get(page_no, []), rect, RE_FIG)
            por_pagina[page_no].append({
                "page": page_no, "bbox": [round(v, 2) for v in
                                          (rect.x0, rect.y0, rect.x1, rect.y1)],
                "file": fpath, "caption": caption,
                "width": round(rect.width, 2), "height": round(rect.height, 2)})
    return por_pagina


# ═══════════════════════════════════════════════════════════════════════════
# FASE 3 · ENSAMBLAJE TEXTUAL
# ═══════════════════════════════════════════════════════════════════════════

def unir_lineas(textos: List[str]) -> str:
    """Misma normalización que el pipeline original (one_line)."""
    t = "\n".join(textos)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    t = re.sub(r"\n+", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def detectar_tipos_requerimiento(text: str) -> List[str]:
    t = (text or "").lower()
    for a, b in zip("áéíóúñ", "aeioun"):
        t = t.replace(a, b)
    words = re.findall(r"[a-z]+", t)
    roots = {
        "solicitar": ["solicit"], "actualizar": ["actualiz"], "aclarar": ["aclar"],
        "rectificar": ["rectific"], "ampliar": ["ampli"], "informar": ["inform"],
        "justificar": ["justific"], "presentar": ["present"], "incorporar": ["incorpor"],
        "complementar": ["complement"], "especificar": ["especific"], "adjuntar": ["adjunt"],
        "verificar": ["verific"], "evaluar": ["evalu"], "revisar": ["revis"],
        "mantener": ["manten", "mantuv", "mantend"], "indicar": ["indic"],
        "senalar": ["senal"], "definir": ["defin"], "describir": ["describ"],
        "identificar": ["identific"], "corroborar": ["corrobor"], "remitir": ["remit"],
        "requerir": ["requier", "requer"], "reiterar": ["reiter"],
        "deber": ["deber", "deba", "debe"],
    }
    found = [lbl for lbl, stems in roots.items()
             if any(any(w.startswith(st) for st in stems) for w in words)]
    return sorted(set(found))


def _procesar_tabla_logica(doc, pdf_path, partes_lineas: List[List[Dict]],
                           tables_dir: str, n_tabla: int,
                           lineas_por_pagina: Dict[int, List[Dict]],
                           tables_document: Dict) -> Dict:
    """Una tabla lógica = uno o más tramos de líneas (partida por páginas).
    Extrae cada parte con la cascada y fusiona las filas."""
    all_rows: List[List[Optional[str]]] = []
    parts_meta, methods, captions = [], [], []
    max_cols = 0

    for parte_idx, ls in enumerate(partes_lineas, start=1):
        page_no = ls[0]["page"]
        page = doc[page_no - 1]
        rect = fitz.Rect(min(l["bbox"][0] for l in ls) - 2,
                         min(l["bbox"][1] for l in ls) - 2,
                         max(l["bbox"][2] for l in ls) + 2,
                         max(l["bbox"][3] for l in ls) + 2)
        fname = f"table_{n_tabla:03d}_part{parte_idx}_p{page_no:03d}.png"
        img_path = save_bbox_screenshot(doc, page_no - 1, rect, tables_dir, fname)
        caption = find_caption(lineas_por_pagina.get(page_no, []), rect, RE_TABLA)
        if caption:
            captions.append(caption)
        vect_text = page.get_text("text", clip=rect).strip()

        td = extract_table_structured(page, rect, pdf_path, page_no - 1, img_path)
        method = td.get("method") if td else "text_only"
        methods.append(method)
        rows = td["rows"] if td else [[l["text"].strip()] for l in ls]
        for r in rows:
            all_rows.append(list(r))
            max_cols = max(max_cols, len(r))

        parts_meta.append({"page": page_no, "image_path": img_path,
                           "bbox_pdf": rect_to_dict(rect),
                           "extraction_method": method})
        tables_document["tables"].append({
            "id": f"t{n_tabla:03d}_part{parte_idx}",
            "page": page_no, "table_index_on_page": parte_idx,
            "caption": caption,
            "detection_methods": ["ai_segmentation"],
            "extraction_method": method,
            "bbox_pdf": rect_to_dict(rect),
            "image_path": img_path,
            "text_digital_pdf": vect_text,
            "table_md": table_data_to_markdown(td) or _fallback_text_to_markdown(vect_text),
            "ocr_text_from_image": ""})

    for r in all_rows:
        while len(r) < max_cols:
            r.append(None)
    merged_td = {"rows": all_rows, "num_rows": len(all_rows),
                 "num_cols": max_cols, "method": "+".join(dict.fromkeys(methods))}
    return {"table_file": parts_meta[0]["image_path"] if parts_meta else None,
            "rows": all_rows,
            "table_md": table_data_to_markdown(merged_td),
            "caption": captions[0] if captions else None,
            "extraction_method": merged_td["method"],
            "pages": [p["page"] for p in parts_meta],
            "parts": parts_meta}


def ensamblar(doc, pdf_path, lineas, segmentos, out_dir, stem) -> Tuple[List[Dict], Dict]:
    orden = {l["id"]: i for i, l in enumerate(lineas)}
    lineas_por_pagina: Dict[int, List[Dict]] = defaultdict(list)
    for l in lineas:
        lineas_por_pagina[l["page"]].append(l)

    tables_dir = os.path.join(out_dir, "tables")
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(tables_dir, exist_ok=True)

    tables_document: Dict[str, Any] = {
        "pdf_path": pdf_path, "tables_dir": tables_dir, "tables": [],
        "extraction_methods_available": [
            "heuristic_grid", "pymupdf_find_tables",
            "pdfplumber" if PDFPLUMBER_AVAILABLE else "(pdfplumber no disponible)",
            "ocr_screenshot" if TESSERACT_AVAILABLE else "(tesseract no disponible)",
            "text_only (fallback)"],
        "ocr_enabled": TESSERACT_AVAILABLE}

    def rango(s):
        return lineas[orden[s["desde"]]:orden[s["hasta"]] + 1]

    obs_map: Dict[str, Dict] = {}
    orden_obs: List[str] = []
    sec1 = sec2 = None
    obs_actual: Optional[str] = None
    n_tabla = 0
    tabla_pend: List[List[Dict]] = []
    tabla_pend_obs: Optional[str] = None

    # Subsección "huérfana": seccion_2 con texto sustantivo que nunca recibe
    # observaciones hijas (ej. "3.1 En relación con... no se intervendrán...").
    # Se promueve a observación propia para no perder su contenido.
    MIN_CHARS_SEC2_HUERFANA = 150
    RE_NUM_PREFIJO = re.compile(r"^\s*((?:\d+\.)*\d+\.?)(?=\s)")
    sec2_pend: Optional[Dict] = None  # {"texto", "ls", "sec1"}

    def promover_sec2_huerfana():
        nonlocal sec2_pend
        pend, sec2_pend = sec2_pend, None
        if not pend or len(pend["texto"]) < MIN_CHARS_SEC2_HUERFANA:
            return
        m = RE_NUM_PREFIJO.match(pend["texto"])
        oid = (m.group(1) if m else f"SEC-{len(orden_obs) + 1}").strip()
        if oid in obs_map:
            return
        obs_map[oid] = {"observation_id": oid, "section_1": pend["sec1"],
                        "section_2": None, "lineas": list(pend["ls"]),
                        "tables": [], "images": []}
        orden_obs.append(oid)

    def cerrar_tabla_pendiente():
        nonlocal n_tabla, tabla_pend, tabla_pend_obs
        if tabla_pend and tabla_pend_obs and tabla_pend_obs in obs_map:
            n_tabla += 1
            obs_map[tabla_pend_obs]["tables"].append(
                _procesar_tabla_logica(doc, pdf_path, tabla_pend, tables_dir,
                                       n_tabla, lineas_por_pagina, tables_document))
        tabla_pend, tabla_pend_obs = [], None

    for s in segmentos:
        tipo = s["tipo"]
        if tipo == "ruido":
            continue  # no cierra la tabla pendiente (pies de página intermedios)
        ls = rango(s)
        if tipo == "tabla" and obs_actual:
            if tabla_pend_obs not in (None, obs_actual):
                cerrar_tabla_pendiente()
            tabla_pend.append(ls)
            tabla_pend_obs = obs_actual
            continue
        cerrar_tabla_pendiente()

        if tipo == "contexto":
            continue
        if tipo == "seccion_1":
            promover_sec2_huerfana()
            sec1 = unir_lineas([l["text"] for l in ls]); sec2 = None; obs_actual = None
        elif tipo == "seccion_2":
            promover_sec2_huerfana()
            sec2 = unir_lineas([l["text"] for l in ls]); obs_actual = None
            sec2_pend = {"texto": sec2, "ls": ls, "sec1": sec1}
        elif tipo == "observacion":
            sec2_pend = None  # la subsección tuvo hijas: es bisagra, no huérfana
            oid = (s.get("id") or f"OBS-{len(orden_obs) + 1}").strip()
            if oid not in obs_map:
                obs_map[oid] = {"observation_id": oid, "section_1": sec1,
                                "section_2": sec2, "lineas": [],
                                "tables": [], "images": []}
                orden_obs.append(oid)
            obs_map[oid]["lineas"].extend(ls)
            obs_actual = oid
        elif tipo == "figura" and obs_actual:
            obs_map[obs_actual]["images"].append(
                {"image_file": None,
                 "caption": unir_lineas([l["text"] for l in ls])})
    cerrar_tabla_pendiente()
    promover_sec2_huerfana()

    # ── Imágenes reales del PDF: extraer y asociar a observaciones ──────────
    imagenes = extraer_imagenes_doc(doc, images_dir, lineas_por_pagina)

    linea_a_obs: Dict[str, str] = {}
    for oid in orden_obs:
        for l in obs_map[oid]["lineas"]:
            linea_a_obs[l["id"]] = oid

    for page_no, imgs in imagenes.items():
        for img in imgs:
            icy = (img["bbox"][1] + img["bbox"][3]) / 2.0
            best_oid, best_d = None, 1e9
            for l in lineas_por_pagina.get(page_no, []):
                oid = linea_a_obs.get(l["id"])
                if not oid:
                    continue
                lcy = (l["bbox"][1] + l["bbox"][3]) / 2.0
                d = abs(icy - lcy)
                if d < best_d:
                    best_d, best_oid = d, oid
            entry = {"image_file": img["file"], "caption": img["caption"],
                     "page": page_no, "bbox": img["bbox"]}
            if best_oid:
                colocada = False
                for it in obs_map[best_oid]["images"]:
                    if it["image_file"] is None and not colocada:
                        it["image_file"] = img["file"]
                        it["caption"] = it["caption"] or img["caption"]
                        it["page"], it["bbox"] = page_no, img["bbox"]
                        colocada = True
                if not colocada:
                    obs_map[best_oid]["images"].append(entry)

    salida = []
    for oid in orden_obs:
        e = obs_map[oid]
        full = unir_lineas([l["text"] for l in e["lineas"]])
        salida.append({
            "observation_id": e["observation_id"],
            "section_1": e["section_1"],
            "section_2": e["section_2"],
            "requirement_types": detectar_tipos_requerimiento(full),
            "text": full,
            "tables": e["tables"],
            "images": e["images"],
            "_trace": {
                "line_ids": [l["id"] for l in e["lineas"]],
                "pages": sorted({l["page"] for l in e["lineas"]}),
                "bboxes": [{"page": l["page"], "bbox": l["bbox"]} for l in e["lineas"]],
            }})
    tables_document["table_count"] = len(tables_document["tables"])
    return salida, tables_document


# ═══════════════════════════════════════════════════════════════════════════
# FASE 4 · VERIFICACIÓN + RESUMEN
# ═══════════════════════════════════════════════════════════════════════════

def _solo_letras(t: str) -> str:
    return re.sub(r"[\s\-]+", "", t)


def verificar(lineas, segmentos, observaciones) -> Dict[str, Any]:
    rep: Dict[str, Any] = {"fidelidad": [], "cobertura": {}, "ok": True}
    por_id = {l["id"]: l for l in lineas}
    for obs in observaciones:
        originales = [por_id[i]["text"] for i in obs["_trace"]["line_ids"]]
        fiel = _solo_letras(unir_lineas(originales)) == _solo_letras(obs["text"])
        rep["fidelidad"].append({"observation_id": obs["observation_id"], "fiel": fiel})
        if not fiel:
            rep["ok"] = False
    orden = {l["id"]: i for i, l in enumerate(lineas)}
    contador = {l["id"]: 0 for l in lineas}
    for s in segmentos:
        for i in range(orden[s["desde"]], orden[s["hasta"]] + 1):
            contador[lineas[i]["id"]] += 1
    sin_asignar = [k for k, v in contador.items() if v == 0]
    duplicadas = [k for k, v in contador.items() if v > 1]
    rep["cobertura"] = {"total_lineas": len(lineas),
                        "sin_asignar": sin_asignar, "duplicadas": duplicadas}
    if sin_asignar or duplicadas:
        rep["ok"] = False
    rep["resumen"] = {"observaciones": len(observaciones),
                      "con_tablas": sum(1 for o in observaciones if o["tables"]),
                      "fieles": sum(1 for f in rep["fidelidad"] if f["fiel"])}
    return rep


def build_summary(output: List[Dict], tables_document: Dict) -> Dict[str, Any]:
    """Portado del original: indicadores y estadísticas del ICSARA."""
    total_obs = len(output)
    sec1_counts: Dict[str, int] = defaultdict(int)
    sec2_counts: Dict[str, int] = defaultdict(int)
    req_counts: Dict[str, int] = defaultdict(int)
    for obs in output:
        sec1_counts[obs.get("section_1") or "(sin sección)"] += 1
        if obs.get("section_2"):
            sec2_counts[obs["section_2"]] += 1
        for rt in obs.get("requirement_types", []):
            req_counts[rt] += 1
    total_tables = sum(len(o.get("tables", [])) for o in output)
    total_images = sum(len(o.get("images", [])) for o in output)
    text_lengths = [len(o.get("text", "")) for o in output]
    avg_len = round(sum(text_lengths) / len(text_lengths), 1) if text_lengths else 0
    max_len = max(text_lengths) if text_lengths else 0
    max_obs = output[text_lengths.index(max_len)]["observation_id"] if text_lengths else ""
    complexity = {"baja (1-2)": 0, "media (3-5)": 0, "alta (6+)": 0}
    for o in output:
        n = len(o.get("requirement_types", []))
        complexity["baja (1-2)" if n <= 2 else "media (3-5)" if n <= 5 else "alta (6+)"] += 1
    methods: Dict[str, int] = defaultdict(int)
    for t in tables_document.get("tables", []):
        methods[t.get("extraction_method", "unknown")] += 1
    return {
        "total_observaciones": total_obs,
        "total_tablas": total_tables,
        "total_imagenes": total_images,
        "observaciones_con_tablas": sum(1 for o in output if o.get("tables")),
        "observaciones_con_imagenes": sum(1 for o in output if o.get("images")),
        "observaciones_solo_texto": sum(1 for o in output
                                        if not o.get("tables") and not o.get("images")),
        "complejidad_observaciones": complexity,
        "largo_promedio_texto_chars": avg_len,
        "observacion_mas_larga": {"observation_id": max_obs, "chars": max_len},
        "distribucion_seccion_1": dict(sorted(sec1_counts.items(),
                                              key=lambda x: x[1], reverse=True)),
        "distribucion_seccion_2": dict(sorted(sec2_counts.items(),
                                              key=lambda x: x[1], reverse=True)),
        "ranking_requirement_types": [{"tipo": k, "frecuencia": v} for k, v in
                                      sorted(req_counts.items(),
                                             key=lambda x: x[1], reverse=True)],
        "metodos_extraccion_tablas": dict(methods),
    }


# ═══════════════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA DEL PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

def _resolver_credenciales(api_key: Optional[str], model: Optional[str]) -> Tuple[str, str]:
    settings = None
    try:
        from app.core.config import get_settings
        settings = get_settings()
    except Exception:
        pass
    api_key = (api_key or
               (settings.anthropic_validacion_icsara_api_key if settings else "") or
               os.environ.get("ANTHROPIC_ADENDA_VALIDACION_ICSARA_API_KEY", "") or
               os.environ.get("ANTHROPIC_API_KEY", ""))
    model = (model or
             os.environ.get("SEGMENTADOR_MODEL", "").strip() or
             ((settings.anthropic_model or "").strip() if settings else "") or
             DEFAULT_MODEL)
    return api_key, model


def run_extraction_ia(
    pdf_path: Path | str,
    out_dir: Path | str,
    artifact_stem: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    labels_path: Path | str | None = None,
    paginas_por_lote: int = PAGINAS_POR_LOTE,
) -> ExtractionSummary:
    """Segmentación IA end-to-end. Escribe los mismos archivos principales que
    el parser heurístico ({stem}.json, tablas_detectadas.json, resumen.json)
    más los artefactos propios de esta arquitectura.

    `labels_path` permite inyectar segmentos ya etiquetados (omite la IA);
    se usa en pruebas."""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"No existe el PDF: {pdf_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)

    stem = artifact_stem or pdf_path.stem
    api_key, model = _resolver_credenciales(api_key, model)

    doc = fitz.open(str(pdf_path))
    try:
        total_pages = doc.page_count

        _log(f"[1/5] Extrayendo líneas de {stem} ({total_pages} páginas)…")
        lineas = extraer_lineas(doc)
        with open(out_dir / f"{stem}_lineas.json", "w", encoding="utf-8") as f:
            json.dump(lineas, f, ensure_ascii=False, indent=1)
        _log(f"      {len(lineas)} líneas")

        if labels_path:
            _log(f"[2/5] Usando etiquetas de {labels_path}")
            with open(labels_path, encoding="utf-8") as f:
                segmentos = json.load(f)["segmentos"]
            err = _validar_segmentos(segmentos, lineas)
            if err:
                raise RuntimeError(f"Etiquetas inválidas: {err}")
        else:
            _log(f"[2/5] Etiquetando con IA ({model})…")
            segmentos = etiquetar_con_ia(doc, lineas, api_key, model, paginas_por_lote)
        with open(out_dir / f"{stem}_segmentos.json", "w", encoding="utf-8") as f:
            json.dump({"segmentos": segmentos}, f, ensure_ascii=False, indent=1)

        _log("[3/5] Ensamblando observaciones, tablas e imágenes…")
        observaciones, tables_document = ensamblar(doc, str(pdf_path), lineas,
                                                   segmentos, str(out_dir), stem)
        main_json_path = out_dir / f"{stem}.json"
        with open(main_json_path, "w", encoding="utf-8") as f:
            json.dump(observaciones, f, ensure_ascii=False, indent=2)
        tables_json_path = out_dir / "tablas_detectadas.json"
        with open(tables_json_path, "w", encoding="utf-8") as f:
            json.dump(tables_document, f, ensure_ascii=False, indent=2)

        _log("[4/5] Verificando fidelidad y cobertura…")
        reporte = verificar(lineas, segmentos, observaciones)
        with open(out_dir / f"{stem}_verificacion.json", "w", encoding="utf-8") as f:
            json.dump(reporte, f, ensure_ascii=False, indent=2)

        _log("[5/5] Generando resumen…")
        resumen = build_summary(observaciones, tables_document)
        resumen["verificacion"] = {**reporte["resumen"], "ok": reporte["ok"]}
        resumen_json_path = out_dir / "resumen.json"
        with open(resumen_json_path, "w", encoding="utf-8") as f:
            json.dump(resumen, f, ensure_ascii=False, indent=2)

        r = reporte["resumen"]
        _log(f"✔ {r['observaciones']} observaciones · {r['fieles']} fieles · "
             f"{resumen['total_tablas']} tablas · {resumen['total_imagenes']} imágenes · "
             f"OK={reporte['ok']}")
    finally:
        doc.close()

    return ExtractionSummary(
        pages=total_pages,
        observaciones=int(resumen.get("total_observaciones", 0)),
        tablas=int(resumen.get("total_tablas", 0)),
        imagenes=int(resumen.get("total_imagenes", 0)),
        output_dir=out_dir,
        output_json=main_json_path,
        tables_json=tables_json_path,
        summary_json=resumen_json_path,
        images_dir=out_dir / "images",
        tables_dir=out_dir / "tables",
    )
