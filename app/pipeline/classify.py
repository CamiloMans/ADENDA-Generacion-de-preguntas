"""Enriquecedor temÃ¡tico de observaciones ICSARA

Lee la salida del ICSARA PDF Parser v3 (JSON de observaciones) y la
enriquece con clasificaciÃ³n temÃ¡tica basada en taxonomÃ­a SEIA de ~450
keywords organizados en 27 temas.

Entradas:
  - {stem}.json          â†’ JSON de observaciones del parser v3
  - resumen.json         â†’ Resumen del parser v3 (opcional, se regenera)

Salidas:
  - {stem}_clasificado.json  â†’ Observaciones enriquecidas con temas
  - resumen_clasificado.json â†’ Indicadores ampliados con distribuciÃ³n temÃ¡tica

Uso:
  python icsara_clasificador.py
"""

import json
import re
import os
import unicodedata
from pathlib import Path
from collections import Counter, defaultdict
from typing import List, Dict, Any, Optional, Tuple

from app.core.text_normalization import repair_mojibake_data
from app.pipeline.types import ClassificationSummary


# =============================================================================
# CONFIG â€” Ajustar rutas segÃºn tu entorno
# =============================================================================

# Carpeta de salida del parser v3 (contiene {stem}.json, resumen.json, etc.)
PARSER_OUTPUT_DIR = os.getenv("ICSARA_BASE_DIR", str(Path("salida_icsara").resolve()))

# Nombre del PDF procesado (sin extensiÃ³n) â€” debe coincidir con el JSON
PDF_STEM = os.getenv("ICSARA_STEM", "ICSARA")

# Umbral mÃ­nimo de score para asignar un tema
MIN_SCORE = 2

# Peso por zona de coincidencia
PESO_SECCION_1 = 3.0   # section_1 (capÃ­tulo principal)
PESO_SECCION_2 = 5.0   # section_2 (subsecciÃ³n/bisagra)
PESO_TEXTO = 1.0        # text (cuerpo de la observaciÃ³n)

# Regla de multi-principal:
# Todo tema con score >= max(MIN_SCORE, ratio * top_score) se marca como principal
MULTI_PRINCIPAL_RATIO = 0.80


# =============================================================================
# TAXONOMÃA ICSARA â€” 27 TEMAS
# =============================================================================

TAXONOMIA = {
    "CALIDAD_AIRE": {
        "nombre": "Calidad del Aire y Emisiones AtmosfÃ©ricas",
        "keywords": [
            "mp10", "mp2,5", "mp2.5", "pm10", "pm2.5", "pm2,5",
            "material particulado", "so2", "soâ‚‚", "diÃ³xido de azufre",
            "no2", "noâ‚‚", "nox", "diÃ³xido de nitrÃ³geno", "Ã³xidos de nitrÃ³geno",
            "monÃ³xido de carbono", "ozono troposfÃ©rico", "plomo atmosfÃ©rico",
            "benceno", "compuestos orgÃ¡nicos volÃ¡tiles", "cov",
            "emisiones atmosfÃ©ricas", "emisiones fugitivas",
            "calpuff", "aermod", "screen3", "wrf", "calmet",
            "modelaciÃ³n de dispersiÃ³n", "modelo de dispersiÃ³n",
            "isopletas", "rosa de vientos", "estabilidad atmosfÃ©rica",
            "fuente fija", "fuente difusa", "fuente mÃ³vil", "chimenea",
            "factor de emisiÃ³n", "ap-42", "cems",
            "inventario de emisiones",
            "ds 12/2022", "ds 12/2010", "ds 104/2018", "ds 114/2002",
            "ds 115/2002", "ds 112/2002", "ds 136/2000", "ds 05/2023",
            "ds 13/2011", "ds 28/2013", "ds 29/2013", "ds 4/1992",
            "ds 37/2013", "ds 09/2023", "ds 138/2005", "ds 144/1961",
            "norma de calidad del aire", "norma de emisiÃ³n atmosfÃ©rica",
            "zona latente", "zona saturada", "ppda",
            "plan de prevenciÃ³n y descontaminaciÃ³n",
            "sinca", "calidad del aire",
            "percentil 98", "concentraciÃ³n 24h", "promedio anual",
            "estaciÃ³n de monitoreo de calidad del aire",
            "tronadura", "tronaduras", "perforaciÃ³n", "perforaciones",
            "supresor de polvo", "humectaciÃ³n",
            "abatimiento", "polvo",
            "mps",
        ],
    },

    "RUIDO_VIBRACIONES": {
        "nombre": "Ruido y Vibraciones",
        "keywords": [
            "ruido", "vibraciones", "vibraciÃ³n",
            "nivel de presiÃ³n sonora", "nps", "npseq", "npc",
            "decibel", "db(a)", "dba", "db(c)", "dbc",
            "horario diurno", "horario nocturno",
            "ruido de fondo", "ruido residual",
            "barrera acÃºstica", "pantalla acÃºstica",
            "propagaciÃ³n sonora", "modelo acÃºstico",
            "ruido tonal", "ruido impulsivo",
            "ds 38/2011", "ds 38", "ds 146/1997", "ds 146",
            "ne-01", "ne-02",
            "zona i", "zona ii", "zona iii", "zona iv",
            "nch 1619",
            "predicciÃ³n y evaluaciÃ³n de impactos por ruido",
            "isolÃ­nea", "isolÃ­neas", "umbral de afectaciÃ³n",
            "kmz", "archivos digitales",
        ],
    },

    "GEOLOGIA_SUELOS": {
        "nombre": "GeologÃ­a, GeomorfologÃ­a y Suelos",
        "keywords": [
            "geologÃ­a", "geomorfologÃ­a", "suelo", "suelos",
            "capacidad de uso", "clase de uso",
            "taxonomÃ­a usda", "serie de suelos",
            "perfil edÃ¡fico", "horizontes del suelo",
            "textura del suelo", "erosiÃ³n",
            "erosiÃ³n hÃ­drica", "erosiÃ³n eÃ³lica", "cÃ¡rcavas",
            "aptitud agrÃ­cola", "aptitud forestal", "aptitud ganadera",
            "remociÃ³n en masa", "deslizamiento", "subsidencia",
            "sismicidad", "falla geolÃ³gica", "riesgo geolÃ³gico",
            "ciren", "sernageomin",
            "dl 3.557", "dl 3557",
            "contaminaciÃ³n de suelos",
            "permeabilidad del suelo", "pedregosidad",
        ],
    },

    "RECURSO_HIDRICO": {
        "nombre": "Recurso HÃ­drico, HidrologÃ­a e HidrogeologÃ­a",
        "keywords": [
            "recurso hÃ­drico", "recursos hÃ­dricos",
            "hidrologÃ­a", "hidrogeologÃ­a", "hidrogeolÃ³gico",
            "caudal ecolÃ³gico", "caudal mÃ­nimo ecolÃ³gico", "caudal ambiental",
            "derechos de aprovechamiento de aguas",
            "balance hÃ­drico", "cuenca hidrogrÃ¡fica",
            "acuÃ­fero", "zona de recarga", "zona de descarga",
            "nivel freÃ¡tico", "piezometrÃ­a", "conductividad hidrÃ¡ulica",
            "aguas subterrÃ¡neas", "aguas superficiales",
            "calidad de aguas", "cuerpo receptor",
            "riles", "ptas", "planta de tratamiento de aguas",
            "dbo", "dqo", "sst", "nkt",
            "coliformes fecales", "coliformes totales",
            "curva de duraciÃ³n de caudales",
            "modflow", "modelo hidrogeolÃ³gico",
            "nch 1333", "nch 409",
            "ds 90/2000", "ds 90", "ds 46/2002", "ds 46",
            "cÃ³digo de aguas", "dfl 1.122",
            "norma secundaria de calidad de aguas",
            "dga", "direcciÃ³n general de aguas",
            "doh", "direcciÃ³n de obras hidrÃ¡ulicas",
            "siss",
            "pozo", "pozos", "monitoreo de aguas",
            "hidroquÃ­mica", "hidroquÃ­mico",
            "zona de prohibiciÃ³n", "shac",
            "pit lake", "aguas halladas",
            "caudal detrÃ­tico", "canal de contorno",
        ],
    },

    "FLORA_VEGETACION": {
        "nombre": "Flora y VegetaciÃ³n",
        "keywords": [
            "flora", "vegetaciÃ³n", "vegetal",
            "inventario florÃ­stico", "catastro vegetacional",
            "formaciones vegetacionales", "formaciones xerofÃ­ticas",
            "unidades homogÃ©neas de vegetaciÃ³n", "uhv",
            "cobertura vegetal",
            "flora leÃ±osa", "flora no leÃ±osa", "suculentas",
            "matorral", "bosque esclerÃ³filo", "bosque nativo",
            "bosque de preservaciÃ³n",
            "categorÃ­a de conservaciÃ³n", "rce",
            "especie en categorÃ­a", "clasificaciÃ³n de especies",
            "monumento natural", "formaciÃ³n relictual",
            "fotointerpretaciÃ³n",
            "singularidades ambientales", "singularidad",
            "revegetaciÃ³n", "reforestaciÃ³n", "restauraciÃ³n ecolÃ³gica",
            "ley 20.283", "bosque nativo",
            "ds 68/2009", "dl 701", "ds 4.363",
            "pas 148", "pas 149", "pas 150", "pas 151",
            "pas 152", "pas 153",
            "artÃ­culo 148", "artÃ­culo 149", "artÃ­culo 150",
            "artÃ­culo 151", "artÃ­culo 152", "artÃ­culo 153",
            "conaf",
            "relevancia ambiental", "germoplasma",
            "desierto florido", "geÃ³fitas",
            "servicios ecosistÃ©micos", "ecosistema",
            "cambio climÃ¡tico",
            "cordia decandra", "eulychnia acida",
            "reclutamiento", "regeneraciÃ³n",
        ],
    },

    "FAUNA": {
        "nombre": "Fauna Terrestre",
        "keywords": [
            "fauna", "fauna silvestre", "fauna terrestre",
            "ensamble faunÃ­stico",
            "categorÃ­a de conservaciÃ³n fauna",
            "herpetofauna", "mastofauna", "avifauna", "entomofauna",
            "aves", "mamÃ­feros", "reptiles", "anfibios",
            "murciÃ©lagos", "quirÃ³pteros",
            "trampa sherman", "trampa tomahawk", "trampa de foso", "pitfall",
            "cÃ¡mara trampa", "fotomonitoreo",
            "transecto lineal", "punto de conteo",
            "rescate y relocalizaciÃ³n", "relocalizaciÃ³n de fauna",
            "corredor biolÃ³gico", "endemismo",
            "plan de manejo de fauna", "perturbaciÃ³n controlada",
            "nidificaciÃ³n", "migraciÃ³n", "reproducciÃ³n fauna",
            "riqueza de especies", "diversidad shannon",
            "ley 19.473", "ley de caza", "ds 5/1998", "cites",
            "pas 146", "pas 147", "pas 123", "pas 124",
            "artÃ­culo 146", "artÃ­culo 147",
            "sag",
            "guanaco", "vizcacha", "lagidium",
            "hÃ¡bitat de relevancia", "hÃ¡bitat",
            "madriguera", "madrigueras",
            "esterilizaciÃ³n", "perros",
        ],
    },

    "ECOSISTEMAS_ACUATICOS": {
        "nombre": "Ecosistemas AcuÃ¡ticos Continentales y Humedales",
        "keywords": [
            "ecosistema acuÃ¡tico", "ecosistemas acuÃ¡ticos",
            "limnologÃ­a", "limnolÃ³gico",
            "fauna Ã­ctica", "ictiofauna", "peces continentales",
            "macroinvertebrados bentÃ³nicos", "macroinvertebrados",
            "fitoplancton", "zooplancton", "macrÃ³fitas",
            "humedal", "humedales", "bofedal", "bofedales",
            "vega", "vegas", "turbera", "turberas",
            "ley 21.202",
            "pas 155", "pas 156", "pas 157", "pas 158", "pas 159",
            "artÃ­culo 155", "artÃ­culo 156", "artÃ­culo 157",
            "modificaciÃ³n de cauce", "obra hidrÃ¡ulica",
        ],
    },

    "PATRIMONIO_CULTURAL": {
        "nombre": "Patrimonio Cultural, ArqueologÃ­a y PaleontologÃ­a",
        "keywords": [
            "patrimonio cultural", "patrimonio arqueolÃ³gico",
            "arqueologÃ­a", "arqueolÃ³gico", "arqueolÃ³gica",
            "paleontologÃ­a", "paleontolÃ³gico", "paleontolÃ³gica",
            "monumento histÃ³rico", "monumento arqueolÃ³gico",
            "santuario de la naturaleza",
            "consejo de monumentos nacionales", "cmn",
            "prospecciÃ³n superficial", "pozo de sondeo",
            "excavaciÃ³n arqueolÃ³gica", "rescate arqueolÃ³gico",
            "monitoreo arqueolÃ³gico", "hallazgo fortuito",
            "hallazgo", "hallazgos",
            "ley 17.288", "ds 484/1990", "ley 21.600",
            "pas 131", "pas 132", "pas 133", "pas 120",
            "artÃ­culo 131", "artÃ­culo 132", "artÃ­culo 133",
            "ficha de registro", "planilla de registro",
            "ciahn", "instituciÃ³n depositaria",
        ],
    },

    "PAISAJE": {
        "nombre": "Paisaje y Valor TurÃ­stico",
        "keywords": [
            "paisaje", "paisajÃ­stico", "valor paisajÃ­stico",
            "calidad visual", "fragilidad visual",
            "cuenca visual", "unidad de paisaje",
            "punto de observaciÃ³n",
            "intrusiÃ³n visual", "obstrucciÃ³n visual",
            "simulaciÃ³n visual", "fotomontaje",
            "valor turÃ­stico", "turÃ­stico",
            "zoit", "sernatur",
        ],
    },

    "AREAS_PROTEGIDAS": {
        "nombre": "Ãreas Protegidas y Sitios Prioritarios",
        "keywords": [
            "Ã¡rea protegida", "Ã¡reas protegidas",
            "snaspe", "parque nacional", "reserva nacional",
            "sitio prioritario", "sitios prioritarios",
            "amcp-mu", "parque marino",
            "bien nacional protegido",
            "objeto de protecciÃ³n", "objetos de protecciÃ³n",
            "simbio", "sbap",
            "pas 120", "pas 121", "pas 130",
        ],
    },

    "MEDIO_HUMANO": {
        "nombre": "Medio Humano",
        "keywords": [
            "medio humano",
            "dimensiÃ³n geogrÃ¡fica", "dimensiÃ³n demogrÃ¡fica",
            "dimensiÃ³n antropolÃ³gica", "dimensiÃ³n socioeconÃ³mica",
            "grupos humanos", "grupo humano",
            "sistemas de vida y costumbres",
            "pueblo indÃ­gena", "pueblos indÃ­genas",
            "ghppi", "comunidad indÃ­gena",
            "reasentamiento",
            "alteraciÃ³n significativa sistemas de vida",
            "convenio 169", "oit",
            "consulta indÃ­gena",
            "conadi",
            "percepciÃ³n comunitaria",
            "territorio indÃ­gena",
            "actividades econÃ³micas locales",
            "mesa de trabajo", "municipalidad",
        ],
    },

    "USO_TERRITORIO": {
        "nombre": "Uso del Territorio y PlanificaciÃ³n Territorial",
        "keywords": [
            "uso del territorio", "planificaciÃ³n territorial",
            "instrumento de planificaciÃ³n territorial", "ipt",
            "plan regulador comunal", "prc",
            "zonificaciÃ³n", "uso de suelo",
            "compatibilidad territorial",
            "oguc", "subdivisiÃ³n predial", "cambio de uso de suelo",
            "pas 160", "pas 161",
            "artÃ­culo 160", "artÃ­culo 161",
            "seremi minvu", "minvu",
        ],
    },

    "DESCRIPCION_PROYECTO": {
        "nombre": "DescripciÃ³n del Proyecto",
        "keywords": [
            "descripciÃ³n del proyecto",
            "partes y obras", "partes, obras y acciones",
            "fase de construcciÃ³n", "fase de operaciÃ³n", "fase de cierre",
            "cronograma", "vida Ãºtil",
            "mano de obra", "monto de inversiÃ³n",
            "suministros bÃ¡sicos", "insumos",
            "layout", "planos", "coordenadas utm",
            "tipologÃ­a de ingreso", "artÃ­culo 10",
            "modificaciÃ³n de proyecto", "artÃ­culo 12",
            "inicio de ejecuciÃ³n",
            "depÃ³sito de relave", "botadero de estÃ©riles",
            "planta de procesos", "rajo",
            "frente de trabajo", "instalaciÃ³n de faena",
            "camino temporal", "laboratorio",
        ],
    },

    "AREA_INFLUENCIA": {
        "nombre": "Ãrea de Influencia",
        "keywords": [
            "Ã¡rea de influencia", "area de influencia",
            "delimitaciÃ³n del Ã¡rea de influencia",
            "justificaciÃ³n del Ã¡rea de influencia",
            "criterios de delimitaciÃ³n",
            "polÃ­gono de Ã¡rea de influencia",
            "radio de influencia",
            "Ã¡rea de estudio",
        ],
    },

    "RESIDUOS": {
        "nombre": "Residuos SÃ³lidos y Peligrosos",
        "keywords": [
            "residuo", "residuos",
            "rsd", "residuos sÃ³lidos domiciliarios",
            "residuos industriales no peligrosos",
            "respel", "residuos peligrosos",
            "plan de manejo de residuos",
            "sidrep", "sinader",
            "disposiciÃ³n final", "valorizaciÃ³n", "reciclaje",
            "relleno sanitario", "bodega respel",
            "toxicidad", "inflamabilidad", "corrosividad",
            "ds 148/2003", "ds 148",
            "ley 20.920", "ley rep",
            "ds 594/1999", "ds 594",
            "pas 138", "pas 139", "pas 140", "pas 141",
            "pas 142", "pas 143", "pas 144", "pas 145",
            "artÃ­culo 138", "artÃ­culo 139", "artÃ­culo 140",
            "artÃ­culo 141", "artÃ­culo 142", "artÃ­culo 143",
            "lodos", "cancha de secado", "impermeabilizaciÃ³n",
            "peligrosidad", "anÃ¡lisis de peligrosidad",
            "almacenamiento de residuos", "patio de residuos",
        ],
    },

    "SUSTANCIAS_PELIGROSAS": {
        "nombre": "Sustancias Peligrosas",
        "keywords": [
            "sustancias peligrosas", "suspel",
            "hoja de datos de seguridad", "hds",
            "nch 382",
            "cubeto de contenciÃ³n", "contenciÃ³n secundaria",
            "bodega de sustancias peligrosas",
            "transporte de cargas peligrosas",
            "ds 43/2015", "ds 78/2009",
        ],
    },

    "TRANSPORTE_VIALIDAD": {
        "nombre": "Transporte y Vialidad",
        "keywords": [
            "transporte", "vialidad",
            "impacto vial", "estudio de impacto vial",
            "generaciÃ³n de viajes", "flujo vehicular",
            "nivel de servicio", "capacidad vial",
            "intersecciÃ³n", "libre circulaciÃ³n",
            "tiempos de desplazamiento",
            "ley 20.958", "ds 30/2017",
            "ley de caminos", "direcciÃ³n de vialidad",
            "ruta", "camiÃ³n", "camiones",
            "preconcentrado", "transporte ferroviario",
            "rca",
        ],
    },

    "PLAN_MEDIDAS": {
        "nombre": "Plan de Medidas de MitigaciÃ³n, ReparaciÃ³n y CompensaciÃ³n",
        "keywords": [
            "plan de medidas", "medida de mitigaciÃ³n",
            "medidas de mitigaciÃ³n", "medida de reparaciÃ³n",
            "medida de compensaciÃ³n", "medidas de compensaciÃ³n",
            "compromiso ambiental voluntario", "cav",
            "compensaciÃ³n de biodiversidad",
            "plan de rescate", "plan de revegetaciÃ³n",
            "equivalencia ecolÃ³gica", "adicionalidad",
            "no pÃ©rdida neta", "permanencia",
            "artÃ­culo 97", "artÃ­culo 98", "artÃ­culo 99",
            "artÃ­culo 100", "artÃ­culo 101", "artÃ­culo 102",
            "guÃ­a para la compensaciÃ³n",
            "rescate y conservaciÃ³n", "rescate arqueolÃ³gico",
            "difusiÃ³n de informaciÃ³n",
            "sobrevivencia", "restablecimiento",
        ],
    },

    "PLAN_CONTINGENCIAS": {
        "nombre": "Plan de Contingencias y Emergencias",
        "keywords": [
            "plan de contingencia", "plan de emergencia",
            "contingencias y emergencias",
            "prevenciÃ³n de contingencias",
            "riesgo ambiental", "riesgos ambientales",
            "derrame", "incendio", "explosiÃ³n", "fuga",
            "protocolo de actuaciÃ³n", "simulacro",
            "sistema de alerta temprana",
            "matriz de riesgos",
            "artÃ­culo 103", "artÃ­culo 104",
        ],
    },

    "PLAN_SEGUIMIENTO": {
        "nombre": "Plan de Seguimiento de Variables Ambientales",
        "keywords": [
            "plan de seguimiento", "seguimiento ambiental",
            "monitoreo ambiental", "variable de seguimiento",
            "frecuencia de muestreo", "punto de monitoreo",
            "puntos de monitoreo",
            "umbrales de acciÃ³n",
            "informe de seguimiento",
            "monitoreo participativo",
            "verificaciÃ³n de cumplimiento",
            "efectividad de medidas",
            "artÃ­culo 105",
        ],
    },

    "NORMATIVA_AMBIENTAL": {
        "nombre": "LegislaciÃ³n y Normativa Ambiental Aplicable",
        "keywords": [
            "normativa ambiental aplicable",
            "legislaciÃ³n ambiental",
            "norma de emisiÃ³n", "normas de emisiÃ³n",
            "norma de calidad", "normas de calidad",
            "norma primaria", "norma secundaria",
            "plan de cumplimiento",
            "consumidor industrial", "ley rep",
            "ley 20.920", "decreto supremo",
            "neumÃ¡ticos",
        ],
    },

    "PARTICIPACION_CIUDADANA": {
        "nombre": "ParticipaciÃ³n Ciudadana",
        "keywords": [
            "participaciÃ³n ciudadana", "pac",
            "observaciones ciudadanas",
            "respuesta a observaciones",
            "adenda ciudadana",
            "participaciÃ³n ciudadana temprana",
            "ponderaciÃ³n de observaciones",
            "inquietudes ciudadanas",
            "lenguaje claro", "comprensible",
        ],
    },

    "CAMBIO_CLIMATICO": {
        "nombre": "Cambio ClimÃ¡tico y GEI",
        "keywords": [
            "cambio climÃ¡tico",
            "gases de efecto invernadero", "gei",
            "co2", "coâ‚‚", "ch4", "n2o",
            "huella de carbono",
            "adaptaciÃ³n al cambio climÃ¡tico",
            "vulnerabilidad climÃ¡tica",
            "escenario rcp", "escenario ssp",
            "ley 21.455", "ley marco cambio climÃ¡tico",
            "precipitaciones", "resiliencia",
            "principio precautorio", "principio de precauciÃ³n",
        ],
    },

    "RIESGO_SALUD": {
        "nombre": "Riesgo para la Salud de la PoblaciÃ³n",
        "keywords": [
            "riesgo para la salud", "riesgo en salud",
            "evaluaciÃ³n de riesgo en salud",
            "vÃ­a de exposiciÃ³n", "vÃ­as de exposiciÃ³n",
            "inhalaciÃ³n", "ingestiÃ³n", "contacto dÃ©rmico",
            "poblaciÃ³n susceptible",
            "latencia", "norma",
            "artÃ­culo 5 rseia",
            "guÃ­a para la evaluaciÃ³n ambiental del riesgo",
        ],
    },

    "MINERIA": {
        "nombre": "Aspectos Mineros",
        "keywords": [
            "minerÃ­a", "minero", "minera",
            "depÃ³sito de relave", "relaves", "relave",
            "botadero de estÃ©riles", "estÃ©riles", "botadero",
            "plan de cierre de faena", "cierre de faena minera",
            "drenaje Ã¡cido", "aguas Ã¡cidas",
            "mineral", "concentrado", "lixiviaciÃ³n",
            "chancado", "molienda", "flotaciÃ³n",
            "pas 135", "pas 136", "pas 137",
            "sernageomin",
            "rajo", "plan minero",
            "preconcentrado de hierro",
            "tronaduras", "perforaciones",
            "caex", "camiÃ³n minero",
        ],
    },

    "PAS": {
        "nombre": "PAS (Permisos Ambientales Sectoriales)",
        "keywords": [
            "permiso ambiental sectorial", "permisos ambientales sectoriales",
        ] + [f"pas {n}" for n in range(111, 162)]
          + [f"artÃ­culo {n}" for n in range(111, 162)],
    },

    "GEOINFORMACION": {
        "nombre": "GeoinformaciÃ³n y CartografÃ­a Digital",
        "keywords": [
            "geoinformaciÃ³n", "cartografÃ­a digital",
            "archivos digitales", "kmz", "shapefile", "shp",
            "mapa de ubicaciÃ³n", "capas",
            "instructivo", "formato zip", "rar",
            "datum", "wgs84", "coordenadas",
        ],
    },
}

TAXONOMIA = repair_mojibake_data(TAXONOMIA)


# =============================================================================
# NORMALIZACIÃ“N
# =============================================================================

def normalizar(texto: str) -> str:
    if not texto:
        return ""
    t = texto.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def normalizar_sin_acentos(texto: str) -> str:
    t = normalizar(texto)
    return "".join(
        char for char in unicodedata.normalize("NFKD", t) if not unicodedata.combining(char)
    )


# =============================================================================
# MOTOR DE CLASIFICACIÃ“N
# =============================================================================

def _log(msg: str) -> None:
    print(msg, flush=True)


def calcular_score_tema(
    tema_data: dict,
    s1_norm: str, s2_norm: str, txt_norm: str,
    s1_sin: str, s2_sin: str, txt_sin: str,
) -> dict:
    """Calcula score de un tema contra las 3 zonas de texto."""
    matches = []
    score = 0.0
    detalle = {"section_1": [], "section_2": [], "text": []}

    for kw in tema_data["keywords"]:
        kw_norm = normalizar(kw)
        kw_sin = normalizar_sin_acentos(kw)

        if len(kw_norm) <= 4:
            pattern = r"\b" + re.escape(kw_norm) + r"\b"
            pattern_sin = r"\b" + re.escape(kw_sin) + r"\b"
        else:
            pattern = re.escape(kw_norm)
            pattern_sin = re.escape(kw_sin)

        found = False

        if s1_norm and (re.search(pattern, s1_norm) or re.search(pattern_sin, s1_sin)):
            score += PESO_SECCION_1
            detalle["section_1"].append(kw)
            found = True

        if s2_norm and (re.search(pattern, s2_norm) or re.search(pattern_sin, s2_sin)):
            score += PESO_SECCION_2
            detalle["section_2"].append(kw)
            found = True

        if txt_norm and (re.search(pattern, txt_norm) or re.search(pattern_sin, txt_sin)):
            score += PESO_TEXTO
            detalle["text"].append(kw)
            found = True

        if found:
            matches.append(kw)

    return {"score": score, "matches": matches, "detalle": detalle}


def clasificar_observacion(obs: dict) -> dict:
    """Clasifica una observaciÃ³n del parser v3 en temas ICSARA."""
    s1 = obs.get("section_1", "") or ""
    s2 = obs.get("section_2", "") or ""
    txt = obs.get("text", "") or ""

    s1_norm = normalizar(s1)
    s2_norm = normalizar(s2)
    txt_norm = normalizar(txt)
    s1_sin = normalizar_sin_acentos(s1)
    s2_sin = normalizar_sin_acentos(s2)
    txt_sin = normalizar_sin_acentos(txt)

    resultados = []
    for tema_id, tema_data in TAXONOMIA.items():
        r = calcular_score_tema(
            tema_data,
            s1_norm, s2_norm, txt_norm,
            s1_sin, s2_sin, txt_sin,
        )
        if r["score"] >= MIN_SCORE:
            resultados.append({
                "id": tema_id,
                "nombre": tema_data["nombre"],
                "score": round(r["score"], 1),
                "keywords_encontradas": r["matches"],
                "detalle_zona": r["detalle"],
            })

    resultados.sort(key=lambda x: x["score"], reverse=True)

    if not resultados:
        return {
            "tema_principal": "Sin clasificar",
            "tema_principal_id": "SIN_CLASIFICAR",
            "temas_principales": [],
            "temas_secundarios": [],
            "score": 0,
            "keywords_match": [],
        }

    top_score = resultados[0]["score"]
    thr = max(MIN_SCORE, MULTI_PRINCIPAL_RATIO * top_score)
    principales = [t for t in resultados if t["score"] >= thr]
    secundarios = [t for t in resultados if t["score"] < thr]

    return {
        "tema_principal": principales[0]["nombre"],
        "tema_principal_id": principales[0]["id"],
        "temas_principales": [
            {"id": t["id"], "nombre": t["nombre"], "score": t["score"]}
            for t in principales
        ],
        "temas_secundarios": [
            {"id": t["id"], "nombre": t["nombre"], "score": t["score"]}
            for t in secundarios[:5]
        ],
        "score": top_score,
        "keywords_match": principales[0]["keywords_encontradas"][:15],
    }


# =============================================================================
# RESUMEN TEMÃTICO
# =============================================================================

def build_resumen_clasificado(
    output_clasificado: List[Dict[str, Any]],
    resumen_parser: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Genera resumen ampliado con distribuciÃ³n temÃ¡tica."""

    total = len(output_clasificado)

    # DistribuciÃ³n por tema principal
    dist_tema = Counter()
    for obs in output_clasificado:
        clf = obs.get("clasificacion", {})
        dist_tema[clf.get("tema_principal", "Sin clasificar")] += 1

    # Complejidad temÃ¡tica (cuÃ¡ntos temas principales por obs)
    multi_tema = sum(
        1 for obs in output_clasificado
        if len(obs.get("clasificacion", {}).get("temas_principales", [])) > 1
    )

    # Observaciones sin clasificar
    sin_clasificar = [
        obs["observation_id"]
        for obs in output_clasificado
        if obs.get("clasificacion", {}).get("tema_principal") == "Sin clasificar"
    ]

    # Keywords mÃ¡s frecuentes globalmente
    all_kw = Counter()
    for obs in output_clasificado:
        for kw in obs.get("clasificacion", {}).get("keywords_match", []):
            all_kw[kw] += 1

    # Cruce: requirement_types Ã— temas
    req_x_tema: Dict[str, Counter] = defaultdict(Counter)
    for obs in output_clasificado:
        tema = obs.get("clasificacion", {}).get("tema_principal", "Sin clasificar")
        for rt in obs.get("requirement_types", []):
            req_x_tema[tema][rt] += 1

    resumen = {
        "total_observaciones": total,
        "observaciones_clasificadas": total - len(sin_clasificar),
        "observaciones_sin_clasificar": len(sin_clasificar),
        "observaciones_multi_tema": multi_tema,
        "distribucion_tematica": [
            {"tema": tema, "observaciones": count}
            for tema, count in dist_tema.most_common()
        ],
        "keywords_mas_frecuentes": [
            {"keyword": kw, "frecuencia": count}
            for kw, count in all_kw.most_common(20)
        ],
        "cruce_tema_requirement_type": {
            tema: dict(reqs.most_common(5))
            for tema, reqs in sorted(req_x_tema.items())
        },
    }

    if sin_clasificar:
        resumen["ids_sin_clasificar"] = sin_clasificar

    # Incluir datos del resumen del parser si existe
    if resumen_parser:
        resumen["datos_parser"] = resumen_parser

    return resumen


def run_classification(
    observaciones_json_path: Path | str,
    out_dir: Path | str,
    artifact_stem: str | None = None,
) -> ClassificationSummary:
    input_json = Path(observaciones_json_path)
    output_dir = Path(out_dir)
    stem = artifact_stem or input_json.stem
    resumen_parser_json = output_dir / "resumen.json"
    output_json = output_dir / f"{stem}_clasificado.json"
    resumen_output_json = output_dir / "resumen_clasificado.json"

    if not input_json.exists():
        raise FileNotFoundError(f"No se encontró: {input_json}")

    observaciones = repair_mojibake_data(json.loads(input_json.read_text(encoding="utf-8")))
    resumen_parser = None
    if resumen_parser_json.exists():
        resumen_parser = repair_mojibake_data(json.loads(resumen_parser_json.read_text(encoding="utf-8")))

    output_clasificado = []
    sin_clf_count = 0
    for obs in observaciones:
        clf = clasificar_observacion(obs)

        output_clasificado.append(
            {
                "observation_id": obs["observation_id"],
                "section_1": obs.get("section_1"),
                "section_2": obs.get("section_2"),
                "requirement_types": obs.get("requirement_types", []),
                "clasificacion": {
                    "tema_principal": clf["tema_principal"],
                    "tema_principal_id": clf["tema_principal_id"],
                    "score": clf["score"],
                    "temas_principales": clf["temas_principales"],
                    "temas_secundarios": clf["temas_secundarios"],
                    "keywords_match": clf["keywords_match"],
                },
                "text": obs.get("text", ""),
                "tables": obs.get("tables", []),
                "images": obs.get("images", []),
            }
        )

        if clf["tema_principal"] == "Sin clasificar":
            sin_clf_count += 1

    output_clasificado = repair_mojibake_data(output_clasificado)
    resumen = repair_mojibake_data(build_resumen_clasificado(output_clasificado, resumen_parser))

    output_json.write_text(
        json.dumps(output_clasificado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    resumen_output_json.write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return ClassificationSummary(
        total=len(output_clasificado),
        classified=len(output_clasificado) - sin_clf_count,
        unclassified=sin_clf_count,
        output_json=output_json,
        output_detail_json=resumen_output_json,
    )


# =============================================================================
# MAIN
# =============================================================================

def main():
    base_dir = Path(PARSER_OUTPUT_DIR)
    input_json = base_dir / f"{PDF_STEM}.json"
    resumen_parser_json = base_dir / "resumen.json"
    output_json = base_dir / f"{PDF_STEM}_clasificado.json"
    resumen_output_json = base_dir / "resumen_clasificado.json"

    _log("â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log("  ICSARA Clasificador TemÃ¡tico v1")
    _log(f"  Entrada: {input_json.name}")
    _log("â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")

    # --- 1. Cargar datos ---
    _log("\n[1/4] Cargando observaciones...")
    if not input_json.exists():
        _log(f"  âœ— No se encontrÃ³: {input_json}")
        return

    observaciones = repair_mojibake_data(json.loads(input_json.read_text(encoding="utf-8")))
    _log(f"       âœ“ {len(observaciones)} observaciones cargadas")
    _log(f"       Temas disponibles: {len(TAXONOMIA)}")

    resumen_parser = None
    if resumen_parser_json.exists():
        resumen_parser = repair_mojibake_data(json.loads(resumen_parser_json.read_text(encoding="utf-8")))
        _log(f"       âœ“ resumen.json cargado")

    # --- 2. Clasificar ---
    _log("\n[2/4] Clasificando observaciones...")
    output_clasificado = []
    sin_clf_count = 0

    for i, obs in enumerate(observaciones, 1):
        clf = clasificar_observacion(obs)

        obs_enriquecida = {
            "observation_id": obs["observation_id"],
            "section_1": obs.get("section_1"),
            "section_2": obs.get("section_2"),
            "requirement_types": obs.get("requirement_types", []),
            "clasificacion": {
                "tema_principal": clf["tema_principal"],
                "tema_principal_id": clf["tema_principal_id"],
                "score": clf["score"],
                "temas_principales": clf["temas_principales"],
                "temas_secundarios": clf["temas_secundarios"],
                "keywords_match": clf["keywords_match"],
            },
            "text": obs.get("text", ""),
            "tables": obs.get("tables", []),
            "images": obs.get("images", []),
        }
        output_clasificado.append(obs_enriquecida)

        if clf["tema_principal"] == "Sin clasificar":
            sin_clf_count += 1

        if i % 10 == 0 or i == len(observaciones):
            _log(f"       â†’ {i}/{len(observaciones)} procesadas")

    _log(f"       âœ“ {len(output_clasificado) - sin_clf_count} clasificadas, {sin_clf_count} sin clasificar")

    # --- 3. Resumen ---
    _log("\n[3/4] Generando resumen...")
    output_clasificado = repair_mojibake_data(output_clasificado)
    resumen = repair_mojibake_data(build_resumen_clasificado(output_clasificado, resumen_parser))

    # --- 4. Guardar ---
    _log("\n[4/4] Guardando archivos...")
    output_json.write_text(
        json.dumps(output_clasificado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    resumen_output_json.write_text(
        json.dumps(resumen, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # --- Reporte final ---
    _log("\nâ•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log("  RESULTADO")
    _log("â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•")
    _log(f"  Observaciones:      {resumen['total_observaciones']}")
    _log(f"  Clasificadas:       {resumen['observaciones_clasificadas']}")
    _log(f"  Sin clasificar:     {resumen['observaciones_sin_clasificar']}")
    _log(f"  Multi-tema:         {resumen['observaciones_multi_tema']}")

    _log(f"\n  DistribuciÃ³n temÃ¡tica:")
    _log("  " + "-" * 55)
    for item in resumen["distribucion_tematica"]:
        bar = "â–ˆ" * min(item["observaciones"], 40)
        _log(f"    {item['tema'][:40]:<40} {item['observaciones']:>3}  {bar}")

    _log(f"\n  Top 10 keywords globales:")
    for item in resumen["keywords_mas_frecuentes"][:10]:
        _log(f"    Â· {item['keyword']:<30} ({item['frecuencia']})")

    if resumen.get("ids_sin_clasificar"):
        _log(f"\n  Obs. sin clasificar: {resumen['ids_sin_clasificar']}")

    _log(f"\n  Archivos generados:")
    _log(f"    â†’ {output_json}")
    _log(f"    â†’ {resumen_output_json}")
    _log("â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•\n")


if __name__ == "__main__":
    main()

