"""ICSARA (SEIA) — Extracción heurística desde PDF a JSON

Descripción:
Proceso heurístico desarrollado para transformar un ICSARA en formato PDF en un dataset
estructurado, trazable y reutilizable. El sistema no se basa en marcadores formales del
documento, sino que **determina** capítulos, bisagras, preguntas y elementos gráficos a
partir de patrones de layout, tipografía, posición relativa y continuidad entre páginas.

Flujo:
  1. Abrir el PDF una única vez y recorrerlo secuencialmente por página.
  2. Determinar tablas vectoriales y figuras raster mediante heurísticas de layout
     (detección por bounding boxes y características gráficas).
  3. Recortar y exportar cada tabla/figura a PNG con nomenclatura estable:
       p{numero_pregunta}_parte{N}_{tabla|figura}.png
  4. Extraer texto plano excluyendo heurísticamente las áreas ocupadas por tablas y figuras,
     evitando duplicación de contenido y ruido visual.
  5. Determinar capítulos mediante heurísticas tipográficas (romanos en negrita, tamaño de
     fuente, posición) y resolver continuidad entre páginas (cross-page).
  6. Determinar bisagras mediante patrones de layout y semántica superficial, asociándolas
     a las preguntas correspondientes.
  7. Limpiar heurísticamente artefactos no textuales: firmas digitales, encabezados
     repetidos y bisagras residuales.
  8. Consolidar cada pregunta en una estructura consistente con su capítulo, bisagra,
     texto limpio y referencias a tablas/figuras asociadas.

Salidas:
  - preguntas.json
      Dataset estructurado por pregunta:
      {capitulo, bisagra, numero, texto,
       tablas_figuras:[{tipo, parte, png}]}

  - preguntas.txt
      Representación legible y continua del contenido textual.

  - outputs_png/
      Recortes de tablas y figuras con trazabilidad directa a cada pregunta.

  - chapters_hinges.json
      Archivo de apoyo para validación y depuración de las heurísticas de detección.
"""

import os
import re
import json
from typing import Any
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path
import fitz  # pymupdf


# =============================================================================
# CONFIG
# =============================================================================
BASE_DIR = Path(os.getenv("ICSARA_BASE_DIR", str(Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd())))
PDF_PATH = Path(os.getenv("ICSARA_PDF_PATH", str(BASE_DIR / "1766432953_2167380849.pdf")))
OUT_DIR = Path(os.getenv("ICSARA_OUT_DIR", str(BASE_DIR / "salida_icsara")))
SEPARADOR_PREGUNTA = "------------"

# --- Texto ---
PAT_PREGUNTA = re.compile(r"(?m)^\s*(\d{1,4})\.\s+")
FRASES_RUIDO = [
    "Para validar las firmas de este documento",
    "sea.gob.cl/validar", "validar las firmas",
    "https://validador.sea.gob.cl/validar",
    "Firmado Digitalmente", "sellodigital.sea.gob.cl",
    "Razón:", "Razon:",
]
FIRMA_TOKENS = (
    "firmado digitalmente", "sellodigital", "utc",
    "fecha:", "razón", "razon", "lugar:",
)
MIN_RATIO_FRECUENTES = 0.60
YEAR_MIN, YEAR_MAX = 1900, 2100
MONO_DROP_THRESHOLD = 5

RE_CAP_ROM_TIT = re.compile(r"^\s*([IVXLCDM]{1,10})\.\s+(.+?)\s*$", re.IGNORECASE)
RE_CAP_ROM_SOLO = re.compile(r"^\s*([IVXLCDM]{1,10})\.\s*$", re.IGNORECASE)
RE_NUM_SOLO = re.compile(r"^\s*\d{1,4}\.\s*$")

RE_TABLA_PARTES = re.compile(r"(?i)^\s*Tabla\s+XX\.\s*Partes\s+y\s+obras\s+del\s+Proyecto\s*$")
RE_NOMBRE_PARTE = re.compile(r"^\s*\[(Nombre\s+parte/obra\s+.+?)\]\s*$", re.IGNORECASE)
RE_CARACTER = re.compile(r"^\s*\[(Temporal\s+o\s+permanente)\]\s*$", re.IGNORECASE)
RE_FASE = re.compile(r"^\s*\[(Construcción.*?cierre)\]\s*$", re.IGNORECASE)

RE_FIRMA_BLOQUE = re.compile(
    r"(?:Fecha:\s*\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s+\d{1,2}:\d{2}[:\d.]*\s*(?:UTC\s*[+-]?\d{2}:\d{2})?\s*(?:Lugar:\s*)?)+",
    re.IGNORECASE,
)
RE_FIRMA_COMPLETA = re.compile(r"(?:Firmado\s+Digitalmente\s+por\s+.+?)(?=\n|$)", re.IGNORECASE)
RE_FECHA_PRE_FIRMA = re.compile(r"\d{1,2}\s+de\s+\w+\s+de\s+\d{4}\.\s*$", re.IGNORECASE)

# --- Detección tablas/figuras ---
THIN_MAX = 1.2
MIN_LINE_LEN = 25.0
MIN_HLINES = 6
MIN_VLINES = 4
MERGE_GAP = 12.0
MIN_TABLE_AREA = 15_000.0
MIN_FIG_AREA = 8_000.0
PNG_DPI = 200
PNG_DIRNAME = "outputs_png"

# --- Layout ---
SAME_LINE_Y = 2.5
SAME_LINE_X_GAP = 22.0
Y_GAP_MERGE = 6.0
X_TOL_MERGE = 24.0
MAX_BISAGRA_TO_Q_GAP = 55.0
BOTTOM_PAGE_MARGIN = 120.0
TOP_NEXT_PAGE_SEARCH = 250.0

RE_ROMAN = re.compile(r"^\s*([IVXLCDM]{1,10})\.\s+(.+?)\s*$", re.IGNORECASE)
RE_QSTART = re.compile(r"^\s*(\d{1,4})\.\s+")


# #############################################################################
#  GEOMETRÍA
# #############################################################################
def rect_area(r):
    return max(0.0, (r.x1 - r.x0)) * max(0.0, (r.y1 - r.y0))

def union_rect(a, b):
    return fitz.Rect(min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))

def intersects(a, b):
    return a.intersects(b)

def rect_close(a, b, gap):
    dx = max(0.0, max(a.x0 - b.x1, b.x0 - a.x1))
    dy = max(0.0, max(a.y0 - b.y1, b.y0 - a.y1))
    return dx <= gap and dy <= gap

def merge_rects(rects, gap=MERGE_GAP):
    rects = [fitz.Rect(r) for r in rects]
    out = []
    for r in rects:
        merged = False
        for i in range(len(out)):
            if rect_close(out[i], r, gap):
                out[i] = union_rect(out[i], r)
                merged = True
                break
        if not merged:
            out.append(r)
    changed = True
    while changed:
        changed = False
        new_out = []
        while out:
            r = out.pop()
            merged_any = False
            for i in range(len(out)):
                if rect_close(out[i], r, gap):
                    out[i] = union_rect(out[i], r)
                    merged_any = True
                    changed = True
                    break
            if not merged_any:
                new_out.append(r)
        out = new_out
    return out

def in_any_rect(point_y0, rects):
    """Verifica si una coordenada y0 cae dentro de algún rect."""
    for r in rects:
        if r.y0 <= point_y0 <= r.y1:
            return True
    return False


# #############################################################################
#  DETECCIÓN TABLAS VECTORIALES Y FIGURAS RASTER
# #############################################################################
def extract_table_candidates(page):
    drawings = page.get_drawings()
    h_lines, v_lines = [], []
    for d in drawings:
        for it in d.get("items", []):
            op = it[0]
            if op == "l":
                (x1, y1), (x2, y2) = it[1], it[2]
                dx, dy = abs(x2 - x1), abs(y2 - y1)
                if dx >= MIN_LINE_LEN and dy <= 1.0:
                    h_lines.append(fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
                elif dy >= MIN_LINE_LEN and dx <= 1.0:
                    v_lines.append(fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
            elif op == "re":
                r = fitz.Rect(it[1])
                w, h = abs(r.x1 - r.x0), abs(r.y1 - r.y0)
                if h <= THIN_MAX and w >= MIN_LINE_LEN:
                    h_lines.append(r)
                elif w <= THIN_MAX and h >= MIN_LINE_LEN:
                    v_lines.append(r)
    if len(h_lines) < MIN_HLINES or len(v_lines) < MIN_VLINES:
        return []
    all_rects = h_lines + v_lines
    bbox = all_rects[0]
    for r in all_rects[1:]:
        bbox = union_rect(bbox, r)
    if rect_area(bbox) < MIN_TABLE_AREA:
        return []
    merged = merge_rects(all_rects, gap=MERGE_GAP)
    groups = merge_rects(merged, gap=MERGE_GAP * 2)
    return [g for g in groups if rect_area(g) >= MIN_TABLE_AREA]


def extract_raster_figures(page):
    figs = []
    for img in page.get_images(full=True):
        xref = img[0]
        for r in page.get_image_rects(xref):
            rr = fitz.Rect(r)
            if rect_area(rr) >= MIN_FIG_AREA:
                figs.append(rr)
    return merge_rects(figs, gap=10.0)


def save_bbox_screenshot(doc, page_index0, bbox, out_dir, fname, dpi=PNG_DPI):
    page = doc[page_index0]
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=fitz.Rect(bbox), alpha=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname
    pix.save(out_path.as_posix())
    return out_path.as_posix()


# #############################################################################
#  TEXTO PLANO — EXCLUYENDO ZONAS DE TABLAS/FIGURAS
# #############################################################################
def extract_page_text_excluding_bboxes(page, exclude_rects):
    """
    Extrae texto de la página excluyendo las zonas de tablas/figuras.
    Usa page.get_text("dict") y filtra bloques/líneas cuyos spans
    caigan dentro de algún rect excluido.
    """
    d = page.get_text("dict")
    out_lines = []

    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_bbox = fitz.Rect(line["bbox"])
            # Si la línea intersecta con algún rect excluido, omitirla
            skip = False
            for er in exclude_rects:
                if intersects(line_bbox, er):
                    skip = True
                    break
            if skip:
                continue
            # Reconstruir texto de la línea
            text = "".join(sp.get("text", "") for sp in line.get("spans", []))
            out_lines.append(text)

    return "\n".join(out_lines)


def normalize_lines_keep_empty(text):
    return [ln.rstrip("\r") for ln in text.replace("\x0c", "\n").split("\n")]


# #############################################################################
#  FILTRO HEADERS/FOOTERS + STITCH
# #############################################################################
def build_frequent_line_filter(pages_lines, min_ratio=MIN_RATIO_FRECUENTES):
    n = len(pages_lines)
    c = Counter()
    for lines in pages_lines:
        for ln in set(ln.strip() for ln in lines if ln.strip()):
            c[ln] += 1
    threshold = max(2, int(n * min_ratio))
    return {ln for ln, k in c.items() if k >= threshold}


def clean_page_lines_keep_empty(lines, frequent_lines):
    out = []
    for ln in lines:
        s = ln.strip()
        if s == "":
            out.append("")
            continue
        if s in frequent_lines:
            continue
        low = s.lower()
        if any(fr.lower() in low for fr in FRASES_RUIDO):
            continue
        if re.fullmatch(r"\d{1,4}", s):
            continue
        out.append(s)
    return out


def stitch_pages(pages_clean_lines):
    texto = ""
    for lines in pages_clean_lines:
        page_text = "\n".join(lines).strip()
        if not page_text:
            continue
        if not texto:
            texto = page_text
            continue
        prev = texto.rstrip()
        if prev.endswith("-"):
            texto = prev[:-1] + page_text.lstrip()
        else:
            texto += "\n" + page_text.lstrip()
    return texto


# #############################################################################
#  NORMALIZACIÓN Y FORMATEO DE TEXTO
# #############################################################################
def normalize_text_preserving_paragraphs(text):
    text = re.sub(r"-\n(?=\w)", "", text)
    text = re.sub(r"\n\s*\n+", "\n<<<PARA>>>\n", text)
    list_start = r"(?:[A-Za-z]\)|\d+\)|\([A-Za-z0-9]+\)|[-•])"
    text = re.sub(rf"\n\s*(?={list_start})", "\n<<<PARA>>>\n", text)
    text = re.sub(r"\n+", " ", text)
    text = text.replace("<<<PARA>>>", "\n")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text


def looks_table_row_horizontal(line):
    if re.search(r"\s{2,}", line.strip()):
        cols = [c for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
        return len(cols) >= 2
    return False


def split_table_row_horizontal(line):
    return [re.sub(r"\s{2,}", " ", c.strip()) for c in re.split(r"\s{2,}", line.strip()) if c.strip()]


def format_horizontal_table_as_semicolon(lines):
    rows = [split_table_row_horizontal(ln) for ln in lines if ln.strip()]
    if not rows:
        return ""
    mx = max(len(r) for r in rows)
    rows = [r + [""] * (mx - len(r)) for r in rows]
    return "\n".join(";".join(r) for r in rows)


def parse_tabla_partes_obras(lines, start_idx):
    i = start_idx
    if i >= len(lines) or not RE_TABLA_PARTES.match(lines[i].strip()):
        return "", start_idx
    table_title = lines[i].strip()
    i += 1
    while i < len(lines) and not RE_NOMBRE_PARTE.match(lines[i].strip()):
        i += 1
    rows = []
    header = ["Tabla", "Nombre", "Descripción", "Carácter", "Fase"]
    while i < len(lines):
        if RE_TABLA_PARTES.match(lines[i].strip()):
            i += 1
            while i < len(lines) and not RE_NOMBRE_PARTE.match(lines[i].strip()):
                i += 1
            continue
        m_name = RE_NOMBRE_PARTE.match(lines[i].strip())
        if not m_name:
            break
        nombre = f"[{m_name.group(1)}]"
        i += 1
        desc_lines = []
        while i < len(lines) and not RE_CARACTER.match(lines[i].strip()):
            if RE_NOMBRE_PARTE.match(lines[i].strip()):
                break
            desc_lines.append(lines[i])
            i += 1
        descripcion = normalize_text_preserving_paragraphs("\n".join(desc_lines)).strip()
        caracter = ""
        if i < len(lines) and RE_CARACTER.match(lines[i].strip()):
            caracter = "Temporal o permanente"
            i += 1
        fase = ""
        if i < len(lines):
            m_f = RE_FASE.match(lines[i].strip())
            if m_f:
                fase = m_f.group(1)
                i += 1
            elif lines[i].strip().startswith("[") and lines[i].strip().endswith("]"):
                fase = lines[i].strip().strip("[]").strip()
                i += 1
        rows.append([table_title, nombre, descripcion, caracter, fase])
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        if i < len(lines) and RE_NUM_SOLO.match(lines[i]):
            break
    if not rows:
        return "", start_idx
    out_lines = [";".join(header)]
    for r in rows:
        out_lines.append(";".join(r))
    return "\n".join(out_lines), i


def format_question(texto_raw):
    lines = normalize_lines_keep_empty(texto_raw)
    out_parts = []
    buf_text = []

    def flush_text():
        nonlocal buf_text
        if not buf_text:
            return
        block = "\n".join(buf_text).strip()
        if block:
            out_parts.append(normalize_text_preserving_paragraphs(block))
        buf_text = []

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "":
            buf_text.append("")
            i += 1
            continue
        if RE_TABLA_PARTES.match(ln.strip()):
            flush_text()
            table_csv, new_i = parse_tabla_partes_obras(lines, i)
            if table_csv:
                out_parts.append(table_csv)
                i = new_i
                continue
        if looks_table_row_horizontal(ln):
            flush_text()
            table_lines = []
            while i < len(lines) and lines[i].strip() != "" and looks_table_row_horizontal(lines[i]):
                table_lines.append(lines[i])
                i += 1
            table_txt = format_horizontal_table_as_semicolon(table_lines)
            if table_txt:
                out_parts.append(table_txt)
            continue
        buf_text.append(ln)
        i += 1
    flush_text()
    return "\n".join(p for p in out_parts if p).strip()


# #############################################################################
#  LIMPIEZA POST-FORMATO
# #############################################################################
def clean_firma_digital(texto):
    texto = RE_FIRMA_BLOQUE.sub("", texto)
    texto = RE_FIRMA_COMPLETA.sub("", texto)
    texto = texto.rstrip()
    m = RE_FECHA_PRE_FIRMA.search(texto)
    if m:
        pos = m.start()
        remaining = RE_FECHA_PRE_FIRMA.sub("", texto[pos:]).strip()
        if not remaining:
            texto = texto[:pos].rstrip()
    return re.sub(r"[\s\n]+$", "", texto)


def clean_trailing_hinge(texto, all_hinge_texts):
    if not all_hinge_texts:
        return texto
    ts = texto.rstrip()
    for ht in all_hinge_texts:
        ht = ht.strip()
        if not ht:
            continue
        if ts.endswith(ht):
            ts = ts[:-len(ht)].rstrip()
            break
        if ht.endswith(".") and ts.endswith(ht[:-1]):
            ts = ts[:-(len(ht) - 1)].rstrip()
            break
    return re.sub(r"[\s\n]+$", "", ts)


# #############################################################################
#  PREGUNTAS — DETECCIÓN DESDE TEXTO PLANO
# #############################################################################
def get_line_bounds(text, pos):
    ls = text.rfind("\n", 0, pos) + 1
    le = text.find("\n", pos)
    return ls, (le if le != -1 else len(text))


def next_nonempty_line(text, start_pos):
    i = start_pos
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        if j == -1:
            return text[i:].strip()
        line = text[i:j].strip()
        if line:
            return line
        i = j + 1
    return ""


def is_false_question_start(texto_total, match_start, num_str):
    ls, le = get_line_bounds(texto_total, match_start)
    line = texto_total[ls:le].strip()
    low = line.lower()
    if len(num_str) == 4:
        try:
            n = int(num_str)
            if YEAR_MIN <= n <= YEAR_MAX and any(t in low for t in FIRMA_TOKENS):
                return True
        except: pass
    only_num = bool(re.fullmatch(r"\s*" + re.escape(num_str) + r"\.\s*", line))
    if not only_num:
        return False
    if len(num_str) == 4:
        try:
            n = int(num_str)
            if YEAR_MIN <= n <= YEAR_MAX:
                return True
        except: pass
    if len(num_str) >= 2 and num_str[0] == "0":
        return True
    nxt = next_nonempty_line(texto_total, le + 1)
    if nxt and not re.match(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", nxt[0]):
        return True
    return False


def apply_monotonic_filter(starts):
    kept = []
    last_num = None
    for ini, num in starts:
        try: n = int(num)
        except: continue
        if last_num is None:
            kept.append((ini, num)); last_num = n; continue
        if n < last_num and (last_num - n) >= MONO_DROP_THRESHOLD:
            continue
        kept.append((ini, num)); last_num = n
    return kept


def extract_questions_from_text(texto_total):
    matches = list(PAT_PREGUNTA.finditer(texto_total))
    starts = []
    for m in matches:
        ini, num = m.start(), m.group(1)
        if not is_false_question_start(texto_total, ini, num):
            starts.append((ini, num))
    starts = apply_monotonic_filter(starts)
    out = []
    for i, (ini, num) in enumerate(starts):
        fin = starts[i + 1][0] if i + 1 < len(starts) else len(texto_total)
        raw_sin = re.sub(rf"^\s*{re.escape(num)}\.\s*", "", texto_total[ini:fin].strip(), count=1)
        out.append({"numero": int(num), "texto": format_question(raw_sin)})
    return out


# #############################################################################
#  LAYOUT — CAPÍTULOS Y BISAGRAS
# #############################################################################
def extract_spans(page):
    d = page.get_text("dict")
    spans = []
    for block in d.get("blocks", []):
        if block.get("type") != 0: continue
        for line in block.get("lines", []):
            for sp in line.get("spans", []):
                txt = (sp.get("text") or "").strip()
                if not txt: continue
                bbox = fitz.Rect(sp["bbox"])
                flags = int(sp.get("flags", 0))
                font = (sp.get("font") or "").lower()
                is_bold = ("bold" in font) or (flags & 16)
                spans.append({"text": txt, "bbox": bbox, "is_bold": bool(is_bold)})
    return spans


def build_lines_from_spans(spans):
    spans = sorted(spans, key=lambda s: (round(s["bbox"].y0, 1), s["bbox"].x0))
    lines = []
    for sp in spans:
        r = sp["bbox"]
        placed = False
        for ln in lines:
            if abs(ln["_y0"] - r.y0) <= SAME_LINE_Y:
                ln["spans"].append(sp)
                ln["_y0"] = (ln["_y0"] + r.y0) / 2.0
                ln["_bbox"] = union_rect(ln["_bbox"], r)
                placed = True; break
        if not placed:
            lines.append({"_y0": r.y0, "_bbox": fitz.Rect(r), "spans": [sp]})
    out = []
    for ln in lines:
        sps = sorted(ln["spans"], key=lambda s: s["bbox"].x0)
        parts = []; prev = None
        for sp in sps:
            if prev is None:
                parts.append(sp["text"])
            else:
                gap = sp["bbox"].x0 - prev["bbox"].x1
                if gap > SAME_LINE_X_GAP:
                    parts.append(" " + sp["text"])
                elif parts and not parts[-1].endswith((" ", "-", "\u201c", "\"", "(", "/")) \
                     and not sp["text"].startswith((",", ".", ")", ":", ";")):
                    parts.append(" " + sp["text"])
                else:
                    parts.append(sp["text"])
            prev = sp
        text = "".join(parts).strip()
        bold_count = sum(1 for sp in sps if sp["is_bold"])
        out.append({"text": text, "bbox": ln["_bbox"], "spans": sps,
                     "is_bold_line": bold_count / max(1, len(sps)) >= 0.6})
    out.sort(key=lambda x: (x["bbox"].y0, x["bbox"].x0))
    return out


def merge_bold_lines(bold_lines):
    if not bold_lines: return []
    bold_lines = sorted(bold_lines, key=lambda x: (x["bbox"].y0, x["bbox"].x0))
    merged = []; cur = None
    for ln in bold_lines:
        if cur is None:
            cur = {"text": ln["text"], "bbox": fitz.Rect(ln["bbox"])}; continue
        dy = ln["bbox"].y0 - cur["bbox"].y1
        same = abs(ln["bbox"].y0 - cur["bbox"].y0) <= SAME_LINE_Y and ln["bbox"].x0 >= cur["bbox"].x0
        nxt = (0.0 <= dy <= Y_GAP_MERGE) and abs(ln["bbox"].x0 - cur["bbox"].x0) <= X_TOL_MERGE
        if same or nxt:
            cur["text"] = (cur["text"] + " " + ln["text"]).strip()
            cur["bbox"] = union_rect(cur["bbox"], ln["bbox"])
        else:
            merged.append(cur); cur = {"text": ln["text"], "bbox": fitz.Rect(ln["bbox"])}
    if cur: merged.append(cur)
    for m in merged: m["text"] = re.sub(r"\s{2,}", " ", m["text"]).strip()
    return merged


def detect_qstarts_layout(lines, exclude_rects, page_no):
    q = []
    for ln in lines:
        skip = False
        for er in exclude_rects:
            if intersects(ln["bbox"], er):
                skip = True; break
        if skip: continue
        m = RE_QSTART.match(ln["text"])
        if m:
            q.append({"num": int(m.group(1)), "bbox": ln["bbox"], "text": ln["text"]})
    q.sort(key=lambda x: (x["bbox"].y0, x["bbox"].x0))
    return q


def classify_bolds(merged_bolds, qstarts, exclude_rects, page_no, page_height, next_qstarts=None):
    chapters, hinges = [], []
    for b in merged_bolds:
        skip = False
        for er in exclude_rects:
            if intersects(b["bbox"], er):
                skip = True; break
        if skip: continue
        txt = b["text"].strip()
        if RE_ROMAN.match(txt):
            chapters.append({"type": "chapter", "page": page_no, "text": txt,
                             "bbox": [b["bbox"].x0, b["bbox"].y0, b["bbox"].x1, b["bbox"].y1],
                             "sort_key": (page_no, b["bbox"].y0)})
            continue
        b_bottom = b["bbox"].y1
        cand = None
        for qs in qstarts:
            if qs["bbox"].y0 < b_bottom: continue
            gap = qs["bbox"].y0 - b_bottom
            if gap <= MAX_BISAGRA_TO_Q_GAP: cand = qs; break
            if gap > MAX_BISAGRA_TO_Q_GAP: break
        if cand is None and next_qstarts and (page_height - b_bottom) <= BOTTOM_PAGE_MARGIN:
            for qs in next_qstarts:
                if qs["bbox"].y0 <= TOP_NEXT_PAGE_SEARCH: cand = qs; break
        if cand is not None:
            hinges.append({"type": "hinge", "page": page_no, "text": txt,
                           "bbox": [b["bbox"].x0, b["bbox"].y0, b["bbox"].x1, b["bbox"].y1],
                           "sort_key": (page_no, b["bbox"].y0)})
    return chapters, hinges


def filter_questions_by_continuity(all_qs):
    if not all_qs: return []
    sorted_qs = sorted(all_qs, key=lambda q: q["sort_key"])
    filtered = []; last = -1
    for q in sorted_qs:
        if last < 0 or q["num"] >= last:
            filtered.append(q); last = q["num"]
    return filtered


def build_hierarchy(chapters, hinges, all_questions):
    timeline = []
    for ch in chapters: timeline.append({"kind": "chapter", "sort_key": ch["sort_key"], "data": ch})
    for h in hinges:   timeline.append({"kind": "hinge",   "sort_key": h["sort_key"],  "data": h})
    for q in all_questions: timeline.append({"kind": "question", "sort_key": q["sort_key"], "data": q})
    timeline.sort(key=lambda x: x["sort_key"])

    result = []; cur_ch = None; cur_h = None

    def fin_h():
        nonlocal cur_h
        if cur_h and cur_ch: cur_ch["hinges"].append(cur_h)
        cur_h = None

    def fin_ch():
        nonlocal cur_ch, cur_h
        fin_h()
        if cur_ch: result.append(cur_ch)
        cur_ch = None

    for item in timeline:
        k = item["kind"]
        if k == "chapter":
            fin_ch()
            d = item["data"]
            cur_ch = {"page": d["page"], "text": d["text"], "bbox": d["bbox"],
                      "questions": [], "hinges": [], "questions_without_hinge": []}
            cur_h = None
        elif k == "hinge":
            if not cur_ch: continue
            fin_h()
            d = item["data"]
            cur_h = {"page": d["page"], "text": d["text"], "bbox": d["bbox"], "questions": []}
        elif k == "question":
            qn = item["data"]["num"]
            if cur_ch:
                cur_ch["questions"].append(qn)
                if cur_h: cur_h["questions"].append(qn)
                else: cur_ch["questions_without_hinge"].append(qn)
    fin_ch()
    return result


def build_question_lookup(hierarchy):
    lookup = {}
    for ch in hierarchy:
        for h in ch["hinges"]:
            for qn in h["questions"]:
                lookup[qn] = {"capitulo": ch["text"], "bisagra": h["text"]}
        for qn in ch["questions_without_hinge"]:
            lookup[qn] = {"capitulo": ch["text"], "bisagra": None}
    return lookup


# #############################################################################
#  ASOCIAR TABLAS/FIGURAS → PREGUNTA (por posición)
# #############################################################################
def find_parent_question(sort_keys, qs_sorted, page, y0):
    idx = bisect_right(sort_keys, (page, y0)) - 1
    return qs_sorted[idx]["num"] if idx >= 0 else None


# #############################################################################
#  SALIDA
# #############################################################################
def save_outputs(out_dir, texto_total, preguntas_final, hierarchy):
    outp = Path(out_dir)
    outp.mkdir(parents=True, exist_ok=True)
    (outp / "texto_total.txt").write_text(texto_total, encoding="utf-8")
    (outp / "preguntas.json").write_text(json.dumps(preguntas_final, ensure_ascii=False, indent=2), encoding="utf-8")
    with (outp / "preguntas.txt").open("w", encoding="utf-8") as f:
        for p in preguntas_final:
            f.write(SEPARADOR_PREGUNTA + "\n")
            if p["capitulo"]: f.write(f"CAPITULO: {p['capitulo']}\n")
            if p["bisagra"]:  f.write(f"BISAGRA: {p['bisagra']}\n")
            f.write(f"NUMERO: {p['numero']}\n")
            f.write(p["texto"] + "\n")
            if p["tablas_figuras"]:
                for tf in p["tablas_figuras"]:
                    f.write(f"  [{tf['tipo'].upper()}] parte {tf['parte']}: {tf['png']}\n")
        f.write(SEPARADOR_PREGUNTA + "\n")
    (outp / "chapters_hinges.json").write_text(
        json.dumps({"chapters": hierarchy}, ensure_ascii=False, indent=2), encoding="utf-8")


# #############################################################################
#  MAIN
# #############################################################################

from app.pipeline.types import ExtractionSummary

def run_extraction(pdf_path: Path | str, out_dir: Path | str, include_png: bool = True) -> ExtractionSummary:
    pdf_path = Path(pdf_path)
    outp = Path(out_dir)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    png_dir = outp / PNG_DIRNAME

    total_pages = len(doc)

    # FASE 1: Detectar tablas/figuras + extraer texto sin tablas
    all_detections = []
    pages_text_clean = []
    all_page_layout_data = []
    exclude_by_page = {}

    for pno in range(total_pages):
        page = doc[pno]
        page_no = pno + 1

        tables = extract_table_candidates(page)
        figs = extract_raster_figures(page)
        excludes = tables + figs
        exclude_by_page[page_no] = excludes

        for r in tables:
            all_detections.append({
                "tipo": "tabla", "page": page_no, "page_idx": pno,
                "bbox": r, "pregunta": None, "parte": None, "png": None,
            })
        for r in figs:
            all_detections.append({
                "tipo": "figura", "page": page_no, "page_idx": pno,
                "bbox": r, "pregunta": None, "parte": None, "png": None,
            })

        page_text = extract_page_text_excluding_bboxes(page, excludes)
        pages_text_clean.append(page_text)

        spans = extract_spans(page)
        lines = build_lines_from_spans(spans)
        bold_lines = [ln for ln in lines if ln["is_bold_line"] and ln["text"]]
        merged_bolds = merge_bold_lines(bold_lines)
        qstarts = detect_qstarts_layout(lines, excludes, page_no)

        all_page_layout_data.append({
            "page_no": page_no,
            "page_height": page.rect.height,
            "merged_bolds": merged_bolds,
            "qstarts": qstarts,
        })

    # FASE 2: Texto plano -> preguntas
    pages_lines = [normalize_lines_keep_empty(t) for t in pages_text_clean]
    frequent_lines = build_frequent_line_filter(pages_lines)
    pages_clean_lines = [clean_page_lines_keep_empty(lines, frequent_lines) for lines in pages_lines]
    texto_total = stitch_pages(pages_clean_lines)
    preguntas_text = extract_questions_from_text(texto_total)

    # FASE 3: Layout -> capitulos, bisagras, jerarquia
    all_chapters, all_hinges = [], []
    for i, pd in enumerate(all_page_layout_data):
        next_qs = all_page_layout_data[i + 1]["qstarts"] if i + 1 < len(all_page_layout_data) else None
        excludes = exclude_by_page.get(pd["page_no"], [])
        chs, hgs = classify_bolds(
            pd["merged_bolds"],
            pd["qstarts"],
            excludes,
            pd["page_no"],
            pd["page_height"],
            next_qstarts=next_qs,
        )
        all_chapters.extend(chs)
        all_hinges.extend(hgs)

    all_qs_raw = []
    for pd in all_page_layout_data:
        for qs in pd["qstarts"]:
            all_qs_raw.append({
                "num": qs["num"],
                "page": pd["page_no"],
                "sort_key": (pd["page_no"], qs["bbox"].y0),
            })

    all_qs_filtered = filter_questions_by_continuity(all_qs_raw)
    hierarchy = build_hierarchy(all_chapters, all_hinges, all_qs_filtered)
    lookup = build_question_lookup(hierarchy)

    # FASE 4: Asociar detecciones -> preguntas + exportar PNGs (opcional)
    sort_keys = [(q["page"], q["sort_key"][1]) for q in sorted(all_qs_filtered, key=lambda q: q["sort_key"])]
    qs_sorted = sorted(all_qs_filtered, key=lambda q: q["sort_key"])
    part_counters = defaultdict(int)

    for det in all_detections:
        parent_q = find_parent_question(sort_keys, qs_sorted, det["page"], det["bbox"].y0)
        det["pregunta"] = parent_q
        q_label = f"{parent_q:03d}" if parent_q is not None else "000"
        part_counters[(q_label, det["tipo"])] += 1
        parte = part_counters[(q_label, det["tipo"])]
        det["parte"] = parte

        fname = f"p{q_label}_parte{parte:03d}_{det['tipo']}.png"
        det["png"] = fname
        if include_png:
            save_bbox_screenshot(doc, det["page_idx"], det["bbox"], png_dir, fname)

    doc.close()

    # FASE 5: Cruzar preguntas + limpiezas + asociar tablas/figuras
    all_hinge_texts = []
    for ch in hierarchy:
        for h in ch["hinges"]:
            all_hinge_texts.append(h["text"])

    dets_by_q = defaultdict(list)
    for det in all_detections:
        if det["pregunta"] is not None:
            dets_by_q[det["pregunta"]].append({
                "tipo": det["tipo"],
                "parte": det["parte"],
                "png": det["png"],
            })

    preguntas_final = []
    for p in preguntas_text:
        num = p["numero"]
        info = lookup.get(num, {})
        texto = clean_trailing_hinge(clean_firma_digital(p["texto"]), all_hinge_texts)
        tf_list = sorted(dets_by_q.get(num, []), key=lambda x: (x["tipo"], x["parte"]))

        preguntas_final.append({
            "capitulo": info.get("capitulo", ""),
            "bisagra": info.get("bisagra"),
            "numero": num,
            "texto": texto,
            "tablas_figuras": tf_list,
        })

    save_outputs(outp, texto_total, preguntas_final, hierarchy)

    n_caps = len(hierarchy)
    n_bis = sum(len(ch["hinges"]) for ch in hierarchy)
    n_preg = len(preguntas_final)
    n_det = len(all_detections)
    n_tab = sum(1 for d in all_detections if d["tipo"] == "tabla")
    n_fig = sum(1 for d in all_detections if d["tipo"] == "figura")

    return ExtractionSummary(
        pages=total_pages,
        capitulos=n_caps,
        bisagras=n_bis,
        preguntas=n_preg,
        tablas=n_tab,
        figuras=n_fig,
        total_detections=n_det,
        output_dir=outp,
    )


def main() -> None:
    if not PDF_PATH.exists():
        print(f"No se encontro el PDF: {PDF_PATH}")
        return
    summary = run_extraction(pdf_path=PDF_PATH, out_dir=OUT_DIR, include_png=True)
    print(f"PDF: {PDF_PATH}")
    print(f"Paginas: {summary.pages}")
    print(f"Capitulos: {summary.capitulos} | Bisagras: {summary.bisagras} | Preguntas: {summary.preguntas}")
    print(f"Tablas: {summary.tablas} | Figuras: {summary.figuras} | Total detecciones: {summary.total_detections}")
    print(f"PNGs en: {summary.output_dir / PNG_DIRNAME}")
    print(f"Salida en: {summary.output_dir}")


if __name__ == "__main__":
    main()
