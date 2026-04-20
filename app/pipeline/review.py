"""
verificador_icsara_v2.py
========================
Verifica que la sistematizaciÃ³n del JSON sea FIEL AL PDF OFICIAL.
El PDF es la fuente de verdad. Claude lo ve directamente.

Correcciones sobre v1:
  - El PDF se lee e integra como fuente primaria (visiÃ³n por pÃ¡ginas)
  - C1: las observaciones faltantes se extraen del PDF, no quedan como marcadores
  - C2/C3: Claude verifica contra el PDF real, no solo por inferencia de contexto
  - El log de auditorÃ­a (_revisado) se escribe en un archivo separado
    para no contaminar el JSON de salida con versiones anteriores
  - El campo "text" en el JSON final siempre contiene una sola versiÃ³n

Lo Ãºnico que se corrige son problemas de EXTRACCIÃ“N:
  C1 - IDs faltantes: se extrae el texto real desde el PDF
  C2 - LÃ­mites de texto: texto cortado o que incluye fragmentos ajenos
       Solo se ajustan los lÃ­mites; el contenido textual no se modifica.
  C3 - Tablas OCR: filas partidas, caracteres basura
       Se fusionan filas y se limpia ruido sin alterar contenido real.

Dependencias: pip install anthropic pymupdf
"""

import json, time, re, copy, os, base64
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import fitz  # PyMuPDF

from app.core.text_normalization import repair_mojibake_data, repair_mojibake_text
from app.pipeline.types import ReviewSummary

# â”€â”€ ConfiguraciÃ³n â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
INPUT_JSON = os.getenv("ICSARA_INPUT_JSON", "")
INPUT_PDF = os.getenv("ICSARA_INPUT_PDF", "")
OUT_JSON = os.getenv("ICSARA_OUTPUT_JSON", "")
OUT_MD = os.getenv("ICSARA_OUTPUT_MD", "")
OUT_AUDIT = os.getenv("ICSARA_OUTPUT_AUDIT", "")
MODEL_ID = os.getenv("ANTHROPIC_MODEL", "")
PDF_DPI = 150

client: Any | None = None


def _build_client(api_key: str) -> Any:
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError("anthropic package is required for the review stage.") from exc
    return Anthropic(api_key=api_key)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# MÃ“DULO PDF
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class LectorPDF:
    """Carga el PDF y expone sus pÃ¡ginas como imÃ¡genes base64 para Claude."""

    def __init__(self, ruta_pdf: str):
        self.doc = fitz.open(ruta_pdf)
        self.n_paginas = len(self.doc)
        print(f"     PDF cargado: {self.n_paginas} pÃ¡ginas â€” {os.path.basename(ruta_pdf)}")

    def pagina_a_base64(self, n: int, dpi: int = PDF_DPI) -> str:
        """Renderiza la pÃ¡gina n (0-indexed) como PNG y retorna base64."""
        mat  = fitz.Matrix(dpi / 72, dpi / 72)
        page = self.doc[n]
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
        return base64.b64encode(pix.tobytes("png")).decode()

    def paginas_b64(self, paginas: list[int]) -> list[str]:
        """Renderiza varias pÃ¡ginas. Filtra Ã­ndices fuera de rango."""
        return [
            self.pagina_a_base64(p)
            for p in paginas
            if 0 <= p < self.n_paginas
        ]

    def texto_pagina(self, n: int) -> str:
        """Texto crudo de la pÃ¡gina (para bÃºsquedas rÃ¡pidas)."""
        return self.doc[n].get_text()

    def buscar_paginas_obs(self, obs_id: str) -> list[int]:
        """
        Devuelve los nÃºmeros de pÃ¡gina (0-indexed) donde aparece obs_id.
        Busca el ID con los formatos habituales del ICSARA.
        """
        variantes = [
            obs_id,
            obs_id.rstrip("."),
            obs_id.replace(".", " "),
        ]
        encontradas = set()
        for n in range(self.n_paginas):
            texto = self.texto_pagina(n)
            if any(v in texto for v in variantes):
                encontradas.add(n)
        # Incluye pÃ¡gina siguiente por si la observaciÃ³n ocupa dos pÃ¡ginas
        return sorted(encontradas | {p + 1 for p in encontradas if p + 1 < self.n_paginas})

    def paginas_rango_obs(self, obs_id: str, next_obs_id: str | None) -> list[int]:
        """
        Rango de pÃ¡ginas probable para una observaciÃ³n.
        Cubre desde la primera pÃ¡gina de obs_id hasta la primera de next_obs_id.
        MÃ¡ximo 4 pÃ¡ginas para no sobrecargar la llamada API.
        """
        inicio = self.buscar_paginas_obs(obs_id)
        if not inicio:
            # Fallback: buscar por nÃºmero de secciÃ³n
            seccion = obs_id.split(".")[0]
            inicio  = [p for p in range(self.n_paginas)
                       if seccion + "." in self.texto_pagina(p)]
        if not inicio:
            return []

        p_inicio = min(inicio)

        if next_obs_id:
            fin = self.buscar_paginas_obs(next_obs_id)
            p_fin = min(fin) if fin else p_inicio + 3
        else:
            p_fin = p_inicio + 3

        return list(range(p_inicio, min(p_fin + 1, p_inicio + 4)))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HELPERS GENERALES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def limpiar_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$",          "", raw)
    return raw


def obs_vacia(obs_id: str, section_1: str = "") -> dict:
    """Estructura mÃ­nima antes de ser rellenada con datos del PDF."""
    return {
        "observation_id": obs_id,
        "section_1": section_1,
        "section_2": None,
        "requirement_types": [],
        "clasificacion": {
            "tema_principal": "Sin clasificar",
            "tema_principal_id": "SIN_CLASIFICAR",
            "score": 0,
            "temas_principales": [],
            "temas_secundarios": [],
            "keywords_match": []
        },
        "text": "",
        "tables": [],
        "images": [],
        "_pendiente_c1": True
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# C1 Â· DETECTAR IDs FALTANTES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def detectar_ids_faltantes(todos_los_ids: list) -> dict:
    prompt = repair_mojibake_text(f"""Eres un experto en documentos ICSARA del SEIA de Chile.

Lista de observation_id extraÃ­dos del PDF:
{json.dumps(todos_los_ids, ensure_ascii=False, indent=2)}

La numeraciÃ³n es jerÃ¡rquica: secciÃ³n.subsecciÃ³n.nÃºmero (ej: 1.1.1, 1.1.2, 1.2.1).
Algunos IDs son de nivel superior sin nÃºmero final (ej: 4.1., 9.1., 13.2.).

Identifica Ãºnicamente los IDs que claramente faltan en la secuencia numÃ©rica.
No marques como faltante un salto que podrÃ­a ser intencional en el documento original.

Responde ÃšNICAMENTE con JSON vÃ¡lido:
{{
  "ids_faltantes": ["IDs que faltan en la secuencia"],
  "ids_anomalos": ["IDs con formato incorrecto o posibles duplicados"],
  "comentario": "explicaciÃ³n breve"
}}""")

    try:
        msg = client.messages.create(
            model=MODEL_ID, max_tokens=1024, temperature=0.1,
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(limpiar_json(msg.content[0].text))
    except Exception as e:
        return {"ids_faltantes": [], "ids_anomalos": [], "error": str(e)}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# C1 Â· EXTRAER OBSERVACIÃ“N FALTANTE DESDE EL PDF
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def extraer_obs_desde_pdf(
    obs_id: str,
    prev_obs: dict | None,
    next_obs: dict | None,
    lector: LectorPDF
) -> dict:
    """
    EnvÃ­a las pÃ¡ginas relevantes del PDF a Claude visiÃ³n y le pide
    que extraiga el texto y las tablas de la observaciÃ³n obs_id.
    Retorna un dict con los campos del JSON (sin clasificacion).
    """
    next_id = next_obs.get("observation_id") if next_obs else None
    paginas = lector.paginas_rango_obs(obs_id, next_id)

    if not paginas:
        # Sin pÃ¡ginas localizadas: marcador mÃ­nimo con advertencia
        return {
            "text": f"[NO LOCALIZADO EN PDF â€” ID {obs_id} no fue encontrado en el documento. Revisar manualmente.]",
            "tables": [],
            "section_2": None,
            "requirement_types": [],
            "_c1_no_localizado": True
        }

    imagenes_b64 = lector.paginas_b64(paginas)

    contexto_prev = (
        f"La observaciÃ³n anterior es {prev_obs['observation_id']} y termina con: "
        f"\"{prev_obs.get('text','')[-300:]}\""
        if prev_obs else "Esta es la primera observaciÃ³n de la secciÃ³n."
    )
    contexto_next = (
        f"La siguiente observaciÃ³n es {next_obs['observation_id']} y comienza con: "
        f"\"{next_obs.get('text','')[:300]}\""
        if next_obs else "No hay observaciÃ³n siguiente conocida."
    )

    # Construir mensaje multimodal: imÃ¡genes + instrucciÃ³n
    content = []
    for i, b64 in enumerate(imagenes_b64):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64}
        })
        content.append({
            "type": "text",
            "text": f"[PÃ¡gina {paginas[i]+1} del PDF]"
        })

    content.append({
        "type": "text",
        "text": repair_mojibake_text(f"""Eres un experto en extracciÃ³n de texto de documentos ICSARA del SEIA de Chile.

Debes extraer FIELMENTE la observaciÃ³n con ID **{obs_id}** de las imÃ¡genes del PDF adjuntas.
No corrijas redacciÃ³n ni ortografÃ­a. Copia el texto exactamente como aparece en el PDF.

Contexto de lÃ­mites:
- {contexto_prev}
- {contexto_next}

Extrae:
1. El texto completo de la observaciÃ³n {obs_id} (sin incluir texto de otras observaciones)
2. Todas las tablas que pertenezcan a esta observaciÃ³n
3. El texto de la subsecciÃ³n (section_2), si aparece (ej: "Medidas de control de polvo")
4. Los tipos de requerimiento si estÃ¡n indicados (ej: "ACLARACIÃ“N", "RECTIFICACIÃ“N", "AMPLIACIÃ“N")

Responde ÃšNICAMENTE con JSON vÃ¡lido:
{{
  "text": "texto completo de la observaciÃ³n tal como aparece en el PDF",
  "section_2": "nombre de subsecciÃ³n o null",
  "requirement_types": ["ACLARACIÃ“N"],
  "tables": [
    {{
      "table_id": "t1",
      "caption": "descripciÃ³n de la tabla o null",
      "rows": [
        ["celda1", "celda2"],
        ["celda3", "celda4"]
      ]
    }}
  ]
}}

Si la observaciÃ³n no tiene tablas, "tables" debe ser [].
Si el texto no fue localizable en las pÃ¡ginas adjuntas, "text" debe ser:
"[NO VISIBLE EN PÃGINAS ADJUNTAS â€” requiere revisiÃ³n manual]"
""")
    })

    try:
        msg = client.messages.create(
            model=MODEL_ID, max_tokens=3000, temperature=0.1,
            messages=[{"role": "user", "content": content}]
        )
        resultado = json.loads(limpiar_json(msg.content[0].text))
        resultado["_c1_extraida_del_pdf"] = True
        resultado["_paginas_consultadas"]  = [p + 1 for p in paginas]
        return resultado
    except json.JSONDecodeError as e:
        return {
            "text": f"[ERROR DE PARSEO JSON EN EXTRACCIÃ“N C1 â€” {e}]",
            "tables": [], "section_2": None, "requirement_types": [],
            "_c1_error": str(e)
        }
    except Exception as e:
        return {
            "text": f"[ERROR API EN EXTRACCIÃ“N C1 â€” {e}]",
            "tables": [], "section_2": None, "requirement_types": [],
            "_c1_error": str(e)
        }


def rellenar_obs_vacia(obs: dict, extraccion: dict) -> dict:
    """Fusiona la estructura obs_vacia con el resultado de extracciÃ³n del PDF."""
    nuevo = copy.deepcopy(obs)
    nuevo["text"]              = extraccion.get("text", nuevo["text"])
    nuevo["tables"]            = extraccion.get("tables", [])
    nuevo["section_2"]         = extraccion.get("section_2")
    nuevo["requirement_types"] = extraccion.get("requirement_types", [])
    nuevo.pop("_pendiente_c1", None)
    # Guardar metadata de extracciÃ³n en _revisado (irÃ¡ al audit log, no al JSON final)
    nuevo["_revisado"] = {
        "c1_extraida_del_pdf": extraccion.get("_c1_extraida_del_pdf", False),
        "c1_paginas": extraccion.get("_paginas_consultadas", []),
        "c1_no_localizado": extraccion.get("_c1_no_localizado", False),
        "c1_error": extraccion.get("_c1_error")
    }
    return nuevo


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# C2 + C3 Â· VERIFICAR EXTRACCIÃ“N POR OBSERVACIÃ“N (con PDF)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def verificar_extraccion(
    obs: dict,
    prev_obs: dict | None,
    next_obs: dict | None,
    lector: LectorPDF | None
) -> dict:
    """
    Claude verifica si la extracciÃ³n fue fiel al PDF.
    Si lector no es None, adjunta las pÃ¡ginas del PDF como referencia visual.

    IMPORTANTE â€” lo que Claude NO debe hacer:
    - Corregir ortografÃ­a o redacciÃ³n
    - Reescribir frases
    - Mejorar el estilo

    C2: verificar lÃ­mites del texto (texto cortado o texto ajeno incluido)
    C3: verificar tablas OCR (filas partidas, caracteres basura)
    """
    prev_text = (prev_obs.get("text", "") if prev_obs else "")[:400]
    next_text = (next_obs.get("text", "") if next_obs else "")[:400]
    tablas_json = json.dumps(obs.get("tables", []), ensure_ascii=False, indent=2)
    obs_id = obs.get("observation_id")

    instruccion = repair_mojibake_text(f"""Eres un experto en revisiÃ³n de extracciÃ³n de texto de PDFs de documentos ICSARA del SEIA de Chile.

Tu misiÃ³n es verificar que la extracciÃ³n del texto y las tablas sea FIEL AL PDF OFICIAL.
NO debes corregir la redacciÃ³n, ortografÃ­a ni el estilo del texto.
Solo debes detectar errores de extracciÃ³n: texto cortado, texto ajeno incluido,
o tablas distorsionadas por OCR.

â•â• OBSERVACIÃ“N A VERIFICAR â•â•
ID: {obs_id}
SECCIÃ“N 1: {obs.get('section_1','')}
SECCIÃ“N 2: {obs.get('section_2','')}
TEXTO EXTRAÃDO: "{obs.get('text','')}"
TABLAS EXTRAÃDAS (JSON):
{tablas_json}

â•â• CONTEXTO DE LÃMITES (textos adyacentes del JSON) â•â•
ID anterior : {prev_obs.get('observation_id','â€”') if prev_obs else 'â€”'}
Texto final : "{prev_text}"

ID siguiente: {next_obs.get('observation_id','â€”') if next_obs else 'â€”'}
Texto inicio: "{next_text}"

â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
CRITERIO C2 â€” FIDELIDAD DEL TEXTO:
EvalÃºa Ãºnicamente si los LÃMITES de extracciÃ³n son correctos:
a) Â¿El texto termina abruptamente? (parte de la observaciÃ³n no fue extraÃ­da)
b) Â¿El texto incluye fragmentos de otra observaciÃ³n?
Si usas las imÃ¡genes del PDF adjuntas, son la fuente de verdad.

Si detectas problema, "c2_texto_corregido" debe ser el texto con lÃ­mites ajustados
SIN cambiar ninguna palabra del contenido propio de esta observaciÃ³n.

CRITERIO C3 â€” FIDELIDAD DE TABLAS:
a) Â¿Hay filas partidas por OCR?
b) Â¿Hay caracteres basura (ej: "g p g", "1 8 2", "S li i l P")?
   Texto fragmentado pero reconocible como parte de una palabra real NO es ruido.
c) Â¿Celdas null donde deberÃ­a haber continuaciÃ³n de fila anterior?

Si hay correcciones de tabla, "c3_tablas_corregidas" debe contener SOLO la versiÃ³n
corregida (no ambas versiones).

Responde ÃšNICAMENTE con JSON vÃ¡lido:
{{
  "hay_problemas": false,
  "c2_texto_fiel": true,
  "c2_problema": null,
  "c2_texto_corregido": null,
  "c3_tablas_fieles": true,
  "c3_problema": null,
  "c3_tablas_corregidas": null
}}

Notas crÃ­ticas:
- "c2_texto_corregido": SOLO el texto corregido, nunca las dos versiones juntas.
- "c3_tablas_corregidas": SOLO el array de tablas corregido, nunca mezcla con el original.
- Si no hay problemas de extracciÃ³n, todos los campos "_corregido/_corregidas" deben ser null.""")

    # Construir content: si hay PDF, adjuntar imÃ¡genes primero
    content = []

    if lector:
        next_id = next_obs.get("observation_id") if next_obs else None
        paginas = lector.paginas_rango_obs(obs_id, next_id)
        if paginas:
            imgs = lector.paginas_b64(paginas)
            for i, b64 in enumerate(imgs):
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64}
                })
                content.append({
                    "type": "text",
                    "text": repair_mojibake_text(f"[PÃ¡gina {paginas[i]+1} del PDF â€” fuente de verdad]")
                })

    content.append({"type": "text", "text": instruccion})

    try:
        msg = client.messages.create(
            model=MODEL_ID, max_tokens=2048, temperature=0.1,
            messages=[{"role": "user", "content": content}]
        )
        result = json.loads(limpiar_json(msg.content[0].text))
        result["observation_id"] = obs_id
        return result
    except json.JSONDecodeError as e:
        return {"observation_id": obs_id,
                "error": f"JSON invÃ¡lido: {e}", "hay_problemas": True}
    except Exception as e:
        return {"observation_id": obs_id,
                "error": str(e), "hay_problemas": True}


def _merge_corrected_items_with_originals(
    originales: Any,
    corregidos: list[Any],
    *,
    ref_fields: tuple[str, ...] = (),
) -> list[Any]:
    originales = originales if isinstance(originales, list) else []
    fusionados: list[Any] = []

    for index, corregido in enumerate(corregidos):
        if not isinstance(corregido, dict):
            fusionados.append(corregido)
            continue

        original = originales[index] if index < len(originales) and isinstance(originales[index], dict) else {}
        fusionado = copy.deepcopy(corregido)

        for key, value in original.items():
            if key not in fusionado:
                fusionado[key] = copy.deepcopy(value)

        for field in ref_fields:
            value = fusionado.get(field)
            if _looks_like_media_ref(value):
                continue

            original_value = original.get(field)
            if isinstance(original_value, str) and original_value.strip():
                fusionado[field] = original_value

        fusionados.append(fusionado)

    return fusionados


def _looks_like_media_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    candidate = value.strip()
    if not candidate:
        return False

    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"}:
        return True

    return bool(Path(candidate).suffix)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# APLICAR CORRECCIONES (sin duplicar versiones)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def aplicar_correcciones(obs: dict, resultado: dict) -> tuple[dict, dict]:
    """
    Retorna (obs_corregida, registro_auditoria).
    El JSON de salida NO incluye versiones anteriores â€” eso va al audit log.
    """
    nuevo   = copy.deepcopy(obs)
    auditoria = {"observation_id": obs.get("observation_id")}
    hay_cambio = False

    # C2: solo ajuste de lÃ­mites
    texto_corregido = resultado.get("c2_texto_corregido")
    if texto_corregido and isinstance(texto_corregido, str):
        # Guardar versiÃ³n anterior en el audit log, no en el JSON principal
        auditoria["c2_texto_anterior"] = nuevo["text"]
        auditoria["c2_problema"]       = resultado.get("c2_problema")
        nuevo["text"] = texto_corregido
        hay_cambio = True

    # C3: solo correcciÃ³n de tablas OCR
    tablas_corregidas = resultado.get("c3_tablas_corregidas")
    if tablas_corregidas is not None and isinstance(tablas_corregidas, list):
        auditoria["c3_tablas_anteriores"] = nuevo.get("tables", [])
        auditoria["c3_problema"]          = resultado.get("c3_problema")
        nuevo["tables"] = _merge_corrected_items_with_originals(
            nuevo.get("tables", []),
            tablas_corregidas,
            ref_fields=("table_file",),
        )
        hay_cambio = True

    # _revisado en el JSON principal: solo estado, sin versiones anteriores
    if hay_cambio:
        nuevo["_revisado"] = {
            "c2_corregido": bool(texto_corregido),
            "c3_corregido": tablas_corregidas is not None
        }
    else:
        nuevo["_revisado"] = {"verificado": True, "sin_cambios": True}

    return nuevo, (auditoria if hay_cambio else {})


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LIMPIAR _revisado DEL JSON FINAL
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def limpiar_revisado_json(data: list) -> list:
    """
    Elimina el campo _revisado del JSON de salida definitivo.
    Toda esa informaciÃ³n queda en el archivo de auditorÃ­a separado.
    """
    limpio = []
    for obs in data:
        obs_limpia = {k: v for k, v in obs.items() if k != "_revisado"}
        limpio.append(obs_limpia)
    return limpio


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# REPORTE MARKDOWN
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def generar_md(resultado_ids: dict, resultados_obs: list, n_total: int,
               n_c1_extraidas: int, n_c1_no_localizadas: int) -> str:
    md = [
        "# Reporte de VerificaciÃ³n de ExtracciÃ³n ICSARA",
        "",
        "> **Criterio base:** el PDF es la fuente de verdad (pÃ¡ginas renderizadas enviadas a Claude visiÃ³n).",
        "> Solo se corrigen errores de extracciÃ³n (lÃ­mites de texto, distorsiones OCR).",
        "> El contenido textual del PDF no se modifica.",
        "",
    ]

    # C1
    md.append("## C1 Â· Completitud de observaciones\n")
    faltantes = resultado_ids.get("ids_faltantes", [])
    anomalos  = resultado_ids.get("ids_anomalos", [])
    if faltantes:
        md.append(f"**IDs no extraÃ­dos en la sistematizaciÃ³n original:** {', '.join(faltantes)}\n")
        md.append(f"- ExtraÃ­dos exitosamente desde el PDF: **{n_c1_extraidas}**")
        md.append(f"- No localizados en el PDF (requieren revisiÃ³n): **{n_c1_no_localizadas}**\n")
    else:
        md.append("âœ“ No se detectaron observaciones faltantes.\n")
    if anomalos:
        md.append(f"**IDs con formato anÃ³malo:** {', '.join(anomalos)}\n")
    if resultado_ids.get("comentario"):
        md.append(f"> {resultado_ids['comentario']}\n")

    # C2 + C3
    md.append("\n## C2 Â· LÃ­mites de texto  |  C3 Â· Fidelidad de tablas\n")
    problemas = [r for r in resultados_obs if r.get("hay_problemas") or r.get("error")]
    sin_prob  = n_total - len(problemas) - len(faltantes)

    md.append(f"- Sin problemas de extracciÃ³n : **{sin_prob}**")
    md.append(f"- Con correcciones aplicadas  : **{len(problemas)}**")
    md.append(f"- Marcadas para revisiÃ³n manual: **{n_c1_no_localizadas}**\n")

    for r in problemas:
        oid = r.get("observation_id", "?")
        md.append(f"\n### {oid}")
        if r.get("error"):
            md.append(f"- âš ï¸ Error al procesar: `{r['error']}`")
            continue
        if not r.get("c2_texto_fiel", True):
            corr = " â†’ correcciÃ³n aplicada" if r.get("c2_texto_corregido") else " â†’ sin correcciÃ³n automÃ¡tica"
            md.append(f"- **[C2 LÃMITES]** {r.get('c2_problema','')}{corr}")
        if not r.get("c3_tablas_fieles", True):
            corr = " â†’ correcciÃ³n aplicada" if r.get("c3_tablas_corregidas") is not None else " â†’ sin correcciÃ³n automÃ¡tica"
            md.append(f"- **[C3 OCR]** {r.get('c3_problema','')}{corr}")

    return "\n".join(md) + "\n"


def run_review(
    input_json_path: Path | str,
    input_pdf_path: Path | str,
    out_dir: Path | str,
    artifact_stem: str,
    *,
    api_key: str,
    model: str,
    pdf_dpi: int = 150,
) -> ReviewSummary:
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for the review stage.")
    if not model:
        raise ValueError("ANTHROPIC_MODEL is required for the review stage.")

    global client, MODEL_ID, PDF_DPI
    client = _build_client(api_key)
    MODEL_ID = model
    PDF_DPI = pdf_dpi

    input_json_path = Path(input_json_path)
    input_pdf_path = Path(input_pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_json = out_dir / f"{artifact_stem}_revisado.json"
    output_md = out_dir / "verificacion_icsara.md"
    output_audit = out_dir / "auditoria_cambios.json"

    with input_json_path.open("r", encoding="utf-8") as handle:
        data = repair_mojibake_data(json.load(handle))
    total = len(data)
    print(f"\nObservaciones cargadas: {total}")

    lector_pdf = None
    if input_pdf_path.exists():
        print(f"\nCargando PDF...")
        lector_pdf = LectorPDF(str(input_pdf_path))
    else:
        print(f"\nPDF no encontrado en {input_pdf_path}")
        print("Se continuara sin verificacion visual contra el PDF.")

    print("\n[C1] Verificando completitud de la secuencia de IDs...")
    todos_ids = [d["observation_id"] for d in data]
    resultado_ids = detectar_ids_faltantes(todos_ids)
    faltantes = resultado_ids.get("ids_faltantes", [])
    print(f"     Faltantes detectados: {faltantes if faltantes else 'ninguno'}")

    data_map = {d["observation_id"]: d for d in data}
    for fid in faltantes:
        if fid not in data_map:
            seccion_num = fid.split(".")[0]
            ref = next((d for d in data if d["observation_id"].startswith(seccion_num + ".")), None)
            s1 = ref["section_1"] if ref else ""
            data_map[fid] = obs_vacia(fid, section_1=s1)

    def sort_key(obs_id: str) -> list[int]:
        return [int(p) for p in obs_id.rstrip(".").split(".") if p.isdigit()]

    all_ids = sorted(data_map.keys(), key=sort_key)
    data_completa = [data_map[i] for i in all_ids]
    total_final = len(data_completa)

    if faltantes:
        print(f"\n[C1] Extrayendo {len(faltantes)} observaciones faltantes desde el PDF...")

    n_c1_extraidas = 0
    n_c1_no_localizadas = 0
    audit_log = []

    for i, obs in enumerate(data_completa):
        if not obs.get("_pendiente_c1"):
            continue

        oid = obs.get("observation_id")
        prev_obs = data_completa[i - 1] if i > 0 else None
        next_obs = data_completa[i + 1] if i < total_final - 1 else None

        print(f"  Extrayendo {oid} del PDF...", end=" ", flush=True)

        if lector_pdf:
            extraccion = extraer_obs_desde_pdf(oid, prev_obs, next_obs, lector_pdf)
        else:
            extraccion = {
                "text": f"[PDF NO DISPONIBLE - la observacion {oid} requiere extraccion manual]",
                "tables": [],
                "section_2": None,
                "requirement_types": [],
                "_c1_no_localizado": True,
            }

        data_completa[i] = rellenar_obs_vacia(obs, extraccion)
        audit_log.append(
            {
                "tipo": "C1_extraccion",
                "observation_id": oid,
                "paginas": extraccion.get("_paginas_consultadas", []),
                "no_localizado": extraccion.get("_c1_no_localizado", False),
            }
        )

        if extraccion.get("_c1_no_localizado") or extraccion.get("_c1_error"):
            n_c1_no_localizadas += 1
            print("no localizado")
        else:
            n_c1_extraidas += 1
            print("extraido")

        time.sleep(1)

    print(f"\n[C2+C3] Verificando fidelidad de extraccion ({total_final} observaciones)...")
    resultados_obs = []
    data_revisada = []

    for i, obs in enumerate(data_completa):
        oid = obs.get("observation_id", "?")

        if obs.get("_revisado", {}).get("c1_no_localizado"):
            print(f"  [{i + 1}/{total_final}] {oid} <- no localizado en PDF, omitir C2/C3")
            data_revisada.append(obs)
            continue

        print(f"  [{i + 1}/{total_final}] {oid}", end=" ", flush=True)

        prev_obs = data_completa[i - 1] if i > 0 else None
        next_obs = data_completa[i + 1] if i < total_final - 1 else None

        resultado = verificar_extraccion(obs, prev_obs, next_obs, lector_pdf)
        resultados_obs.append(resultado)

        obs_corregida, registro_auditoria = aplicar_correcciones(obs, resultado)
        if registro_auditoria:
            audit_log.append({"tipo": "C2C3_correccion", **registro_auditoria})

        data_revisada.append(obs_corregida)

        print("corregida" if resultado.get("hay_problemas") else "ok")
        time.sleep(0.5)

    resultado_ids = repair_mojibake_data(resultado_ids)
    resultados_obs = repair_mojibake_data(resultados_obs)
    audit_log = repair_mojibake_data(audit_log)
    data_final = repair_mojibake_data(limpiar_revisado_json(data_revisada))

    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(data_final, handle, indent=2, ensure_ascii=False)

    with output_audit.open("w", encoding="utf-8") as handle:
        json.dump(audit_log, handle, indent=2, ensure_ascii=False)

    reporte = repair_mojibake_text(
        generar_md(resultado_ids, resultados_obs, total_final, n_c1_extraidas, n_c1_no_localizadas)
    )
    with output_md.open("w", encoding="utf-8") as handle:
        handle.write(reporte)

    n_corr = sum(1 for r in resultados_obs if r.get("hay_problemas"))
    return ReviewSummary(
        total_observaciones=total_final,
        ids_faltantes_detectados=len(faltantes),
        ids_extraidas_desde_pdf=n_c1_extraidas,
        ids_no_localizadas=n_c1_no_localizadas,
        correcciones_revision=n_corr,
        output_json=output_json,
        audit_json=output_audit,
        report_md=output_md,
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# MAIN
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def main():
    if not INPUT_JSON or not INPUT_PDF or not OUT_JSON:
        raise RuntimeError("ICSARA_INPUT_JSON, ICSARA_INPUT_PDF and ICSARA_OUTPUT_JSON are required.")

    out_dir = Path(OUT_JSON).resolve().parent
    artifact_stem = Path(OUT_JSON).stem.replace("_revisado", "")
    run_review(
        INPUT_JSON,
        INPUT_PDF,
        out_dir,
        artifact_stem,
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        model=MODEL_ID,
        pdf_dpi=PDF_DPI,
    )

if __name__ == "__main__":
    main()

