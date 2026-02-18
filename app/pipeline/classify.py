"""Clasificador temático de preguntas ICSARA (SEIA Chile)

Este script lee un archivo preguntas.json y, para cada pregunta, calcula afinidad temática
contra una taxonomía (temas + keywords). El cálculo usa coincidencias de keywords en tres
zonas del texto: capítulo, bisagra y texto principal, asignando pesos distintos por zona.

Para cada pregunta:
- Normaliza los textos para robustecer coincidencias (minúsculas, espacios y una versión sin acentos).
- Evalúa cada tema: detecta keywords presentes y suma puntaje según dónde aparece cada keyword.
- Filtra temas bajo un umbral mínimo para reducir falsos positivos.
- Ordena los temas detectados por score descendente.
- Devuelve una clasificación estructurada con:
  - tema_principal / tema_principal_id (tema con mayor score)
  - temas_principales / temas_principales_id (conjunto de temas que cumplen criterio de principal)
  - temas_secundarios (los siguientes temas más relevantes)
  - keywords_match (evidencia: keywords encontradas)
  - detalle por zona (capítulo/bisagra/texto) para trazabilidad

Finalmente exporta preguntas_clasificadas.json con los campos originales más las etiquetas,
scores y evidencias de coincidencia.

Uso:
  python clasificar_preguntas.py
"""

import os
import json
from typing import Any
import re
from pathlib import Path
from collections import Counter

# =============================================================================
# CONFIG
# =============================================================================
BASE_DIR = Path(os.getenv("ICSARA_BASE_DIR", str(Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd())))
DEFAULT_OUT_DIR = Path(os.getenv("ICSARA_OUT_DIR", str(BASE_DIR / "salida_icsara")))
INPUT_JSON = Path(os.getenv("ICSARA_INPUT_JSON", str(DEFAULT_OUT_DIR / "preguntas.json")))
OUTPUT_JSON = Path(os.getenv("ICSARA_OUTPUT_JSON", str(DEFAULT_OUT_DIR / "preguntas_clasificadas.json")))
OUTPUT_JSON_DETALLE = Path(os.getenv("ICSARA_OUTPUT_JSON_DETALLE", str(DEFAULT_OUT_DIR / "preguntas_clasificadas_detalle.json")))

# Umbral mínimo de score para asignar un tema (evitar falsos positivos)
MIN_SCORE = 2

# Peso por zona de coincidencia
PESO_CAPITULO = 3.0
PESO_BISAGRA = 5.0
PESO_TEXTO = 1.0

# Regla de multi-principal:
# Todo tema con score >= max(MIN_SCORE, MULTI_PRINCIPAL_RATIO * top_score) se marca como principal
MULTI_PRINCIPAL_RATIO = 0.80

# =============================================================================
# TAXONOMÍA ICSARA — DICCIONARIO COMPLETO (con 2 temas nuevos)
# =============================================================================
TAXONOMIA = {
    "CALIDAD_AIRE": {
        "nombre": "Calidad del Aire y Emisiones Atmosféricas",
        "keywords": [
            "mp10", "mp2,5", "mp2.5", "pm10", "pm2.5", "pm2,5",
            "material particulado", "so2", "so₂", "dióxido de azufre",
            "no2", "no₂", "nox", "dióxido de nitrógeno", "óxidos de nitrógeno",
            "monóxido de carbono", "ozono troposférico", "plomo atmosférico",
            "benceno", "compuestos orgánicos volátiles", "cov",
            "emisiones atmosféricas", "emisiones fugitivas",
            "calpuff", "aermod", "screen3", "wrf", "calmet",
            "modelación de dispersión", "modelo de dispersión",
            "isopletas", "rosa de vientos", "estabilidad atmosférica",
            "fuente fija", "fuente difusa", "fuente móvil", "chimenea",
            "factor de emisión", "ap-42", "cems",
            "inventario de emisiones",
            "ds 12/2022", "ds 12/2010", "ds 104/2018", "ds 114/2002",
            "ds 115/2002", "ds 112/2002", "ds 136/2000", "ds 05/2023",
            "ds 13/2011", "ds 28/2013", "ds 29/2013", "ds 4/1992",
            "ds 37/2013", "ds 09/2023", "ds 138/2005", "ds 144/1961",
            "norma de calidad del aire", "norma de emisión atmosférica",
            "zona latente", "zona saturada", "ppda",
            "plan de prevención y descontaminación",
            "sinca", "calidad del aire",
            "percentil 98", "concentración 24h", "promedio anual",
            "estación de monitoreo de calidad del aire",
        ],
    },

    "RUIDO_VIBRACIONES": {
        "nombre": "Ruido y Vibraciones",
        "keywords": [
            "ruido", "vibraciones", "vibración",
            "nivel de presión sonora", "nps", "npseq", "npc",
            "decibel", "db(a)", "dba",
            "horario diurno", "horario nocturno",
            "ruido de fondo", "ruido residual",
            "barrera acústica", "pantalla acústica",
            "propagación sonora", "modelo acústico",
            "ruido tonal", "ruido impulsivo",
            "ruido submarino", "efecto corona",
            "ds 38/2011", "ds 38", "ds 146/1997", "ds 146",
            "ne-01", "ne-02",
            "zona i", "zona ii", "zona iii", "zona iv",
            "nch 1619",
            "predicción y evaluación de impactos por ruido",
        ],
    },

    "GEOLOGIA_SUELOS": {
        "nombre": "Geología, Geomorfología y Suelos",
        "keywords": [
            "geología", "geomorfología", "suelo", "suelos",
            "capacidad de uso", "clase de uso",
            "taxonomía usda", "serie de suelos",
            "perfil edáfico", "horizontes del suelo",
            "textura del suelo", "erosión",
            "erosión hídrica", "erosión eólica", "cárcavas",
            "aptitud agrícola", "aptitud forestal", "aptitud ganadera",
            "remoción en masa", "deslizamiento", "subsidencia",
            "sismicidad", "falla geológica", "riesgo geológico",
            "ciren", "sernageomin",
            "dl 3.557", "dl 3557",
            "contaminación de suelos",
            "permeabilidad del suelo", "pedregosidad",
        ],
    },

    "RECURSO_HIDRICO": {
        "nombre": "Recurso Hídrico, Hidrología e Hidrogeología",
        "keywords": [
            "recurso hídrico", "recursos hídricos",
            "hidrología", "hidrogeología", "hidrogeológico",
            "caudal ecológico", "caudal mínimo ecológico", "caudal ambiental",
            "derechos de aprovechamiento de aguas",
            "balance hídrico", "cuenca hidrográfica",
            "acuífero", "zona de recarga", "zona de descarga",
            "nivel freático", "piezometría", "conductividad hidráulica",
            "aguas subterráneas", "aguas superficiales",
            "calidad de aguas", "cuerpo receptor",
            "riles", "ptas", "planta de tratamiento de aguas",
            "dbo", "dqo", "sst", "nkt",
            "coliformes fecales", "coliformes totales",
            "curva de duración de caudales",
            "modflow", "modelo hidrogeológico",
            "nch 1333", "nch 409",
            "ds 90/2000", "ds 90", "ds 46/2002", "ds 46",
            "código de aguas", "dfl 1.122",
            "norma secundaria de calidad de aguas",
            "dga", "dirección general de aguas",
            "doh", "dirección de obras hidráulicas",
            "siss",
        ],
    },

    "GLACIARES": {
        "nombre": "Glaciares y Criósfera",
        "keywords": [
            "glaciar", "glaciares", "glaciaretes",
            "glaciar rocoso", "glaciares rocosos",
            "criósfera", "permafrost",
            "balance de masa glaciar", "retroceso glaciar",
            "zona periglaciar",
            "inventario de glaciares",
            "unidad de glaciología",
            "estrategia nacional de glaciares",
        ],
    },

    "FLORA_VEGETACION": {
        "nombre": "Flora y Vegetación",
        "keywords": [
            "flora", "vegetación", "vegetal",
            "inventario florístico", "catastro vegetacional",
            "formaciones vegetacionales", "formaciones xerofíticas",
            "unidades homogéneas de vegetación", "uhv",
            "cobertura vegetal",
            "flora leñosa", "flora no leñosa", "suculentas",
            "matorral", "bosque esclerófilo", "bosque nativo",
            "bosque de preservación",
            "categoría de conservación", "rce",
            "especie en categoría", "clasificación de especies",
            "monumento natural", "formación relictual",
            "fotointerpretación",
            "singularidades ambientales",
            "revegetación", "reforestación", "restauración ecológica",
            "ley 20.283", "bosque nativo",
            "ds 68/2009", "dl 701", "ds 4.363",
            "pas 148", "pas 149", "pas 150", "pas 151",
            "pas 152", "pas 153",
            "artículo 148", "artículo 149", "artículo 150",
            "artículo 151", "artículo 152", "artículo 153",
            "pas 127", "pas 128", "pas 129",
            "conaf",
        ],
    },

    "FAUNA": {
        "nombre": "Fauna Terrestre",
        "keywords": [
            "fauna", "fauna silvestre", "fauna terrestre",
            "ensamble faunístico",
            "categoría de conservación fauna",
            "herpetofauna", "mastofauna", "avifauna", "entomofauna",
            "aves", "mamíferos", "reptiles", "anfibios",
            "murciélagos", "quirópteros",
            "trampa sherman", "trampa tomahawk", "trampa de foso", "pitfall",
            "cámara trampa", "fotomonitoreo",
            "red de niebla", "redes de niebla",
            "transecto lineal", "punto de conteo",
            "censo visual", "censo auditivo",
            "rescate y relocalización", "relocalización de fauna",
            "corredor biológico", "endemismo",
            "plan de manejo de fauna", "perturbación controlada",
            "nidificación", "migración", "reproducción fauna",
            "riqueza de especies", "diversidad shannon", "diversidad simpson",
            "ley 19.473", "ley de caza", "ds 5/1998", "cites",
            "pas 146", "pas 147", "pas 123", "pas 124",
            "artículo 146", "artículo 147",
            "sag",
        ],
    },

    "ECOSISTEMAS_ACUATICOS": {
        "nombre": "Ecosistemas Acuáticos Continentales y Humedales",
        "keywords": [
            "ecosistema acuático", "ecosistemas acuáticos",
            "limnología", "limnológico",
            "fauna íctica", "ictiofauna", "peces continentales",
            "macroinvertebrados bentónicos", "macroinvertebrados",
            "fitoplancton", "zooplancton", "macrófitas",
            "índice biótico", "ibf", "ept", "bmwp",
            "hábitat acuático", "ribereño",
            "humedal", "humedales", "bofedal", "bofedales",
            "vega", "vegas", "turbera", "turberas",
            "humedal urbano", "humedales urbanos",
            "inventario nacional de humedales", "ramsar",
            "ley 21.202",
            "ds 15 mma",
            "pas 155", "pas 156", "pas 157", "pas 158", "pas 159",
            "artículo 155", "artículo 156", "artículo 157",
            "artículo 158", "artículo 159",
            "modificación de cauce", "modificaciones de cauce",
            "obra hidráulica", "obras hidráulicas",
            "regularización de cauce", "defensa de cauce",
            "extracción de ripio", "extracción de arena",
        ],
    },

    "ECOSISTEMAS_MARINOS": {
        "nombre": "Ecosistemas Marinos",
        "keywords": [
            "ecosistema marino", "ecosistemas marinos", "medio marino",
            "columna de agua", "sedimentos marinos",
            "biota marina", "macroalgas", "macrofauna bentónica",
            "intermareal", "submareal", "infralitoral",
            "batimetría", "corrientes marinas",
            "amerb", "concesión de acuicultura", "concesión marítima",
            "caleta", "pescadores artesanales",
            "biodiversidad marina", "borde costero",
            "ley 18.892", "lgpa",
            "ds 1/1992",
            "pas 111", "pas 112", "pas 113", "pas 114", "pas 115",
            "pas 116", "pas 117", "pas 118", "pas 119",
            "artículo 111", "artículo 112", "artículo 113",
            "artículo 114", "artículo 115", "artículo 116",
            "subpesca", "sernapesca", "directemar", "shoa",
        ],
    },

    "PATRIMONIO_CULTURAL": {
        "nombre": "Patrimonio Cultural, Arqueología y Paleontología",
        "keywords": [
            "patrimonio cultural", "patrimonio arqueológico",
            "arqueología", "arqueológico", "arqueológica",
            "paleontología", "paleontológico", "paleontológica",
            "monumento histórico", "monumento arqueológico",
            "monumento paleontológico", "monumento público",
            "zona típica", "zonas típicas",
            "santuario de la naturaleza",
            "consejo de monumentos nacionales", "cmn",
            "prospección superficial", "pozo de sondeo",
            "excavación arqueológica", "rescate arqueológico",
            "monitoreo arqueológico", "hallazgo fortuito",
            "artículo 26 ley 17.288",
            "conformidad cmn",
            "patrimonio tangible", "patrimonio intangible",
            "ley 17.288", "ds 484/1990", "ley 21.600",
            "pas 131", "pas 132", "pas 133", "pas 120",
            "artículo 131", "artículo 132", "artículo 133",
            "artículo 120",
        ],
    },

    "PAISAJE": {
        "nombre": "Paisaje y Valor Turístico",
        "keywords": [
            "paisaje", "paisajístico", "valor paisajístico",
            "calidad visual", "fragilidad visual",
            "cuenca visual", "cuencas visuales",
            "unidad de paisaje", "unidades de paisaje",
            "punto de observación", "puntos de observación",
            "carácter del paisaje",
            "intrusión visual", "obstrucción visual",
            "incompatibilidad visual", "artificialidad",
            "simulación visual", "fotomontaje",
            "valor turístico", "turístico",
            "zoit", "zona de interés turístico",
            "atractivo natural", "atractivo cultural",
            "sernatur",
        ],
    },

    "AREAS_PROTEGIDAS": {
        "nombre": "Áreas Protegidas y Sitios Prioritarios",
        "keywords": [
            "área protegida", "áreas protegidas",
            "snaspe", "parque nacional", "reserva nacional",
            "monumento natural",
            "santuario de la naturaleza",
            "sitio ramsar",
            "sitio prioritario", "sitios prioritarios",
            "amcp-mu", "parque marino",
            "bien nacional protegido",
            "acbpo", "área colocada bajo protección oficial",
            "objeto de protección", "objetos de protección",
            "registro nacional de áreas protegidas",
            "simbio", "sbap",
            "pas 120", "pas 121", "pas 130",
            "artículo 121",
        ],
    },

    "MEDIO_HUMANO": {
        "nombre": "Medio Humano",
        "keywords": [
            "medio humano",
            "dimensión geográfica", "dimensión demográfica",
            "dimensión antropológica", "dimensión socioeconómica",
            "dimensión de bienestar social",
            "grupos humanos", "grupo humano",
            "sistemas de vida y costumbres",
            "pueblo indígena", "pueblos indígenas",
            "ghppi", "comunidad indígena",
            "reasentamiento", "reasentamiento de comunidades",
            "alteración significativa sistemas de vida",
            "cargas ambientales",
            "convenio 169", "oit",
            "consulta indígena",
            "conadi",
            "percepción comunitaria",
            "territorio indígena",
            "actividades económicas locales",
        ],
    },

    "USO_TERRITORIO": {
        "nombre": "Uso del Territorio y Planificación Territorial",
        "keywords": [
            "uso del territorio", "planificación territorial",
            "instrumento de planificación territorial", "ipt",
            "plan regulador comunal", "prc",
            "plan regulador intercomunal", "pri",
            "plan regional de ordenamiento territorial", "prot",
            "zonificación", "uso de suelo",
            "compatibilidad territorial",
            "oguc", "ordenanza general",
            "subdivisión predial", "cambio de uso de suelo",
            "terreno rural", "límite urbano",
            "pas 160", "pas 161",
            "artículo 160", "artículo 161",
            "calificación de instalaciones industriales",
            "seremi minvu", "minvu",
        ],
    },

    "DESCRIPCION_PROYECTO": {
        "nombre": "Descripción del Proyecto",
        "keywords": [
            "descripción del proyecto",
            "partes y obras", "partes, obras y acciones",
            "fase de construcción", "fase de operación", "fase de cierre",
            "cronograma", "vida útil",
            "mano de obra", "monto de inversión",
            "suministros básicos", "insumos",
            "capacidad instalada", "tecnología",
            "layout", "planos", "coordenadas utm",
            "datum wgs84",
            "tipología de ingreso", "artículo 10",
            "modificación de proyecto", "artículo 12",
            "desarrollo por etapas", "artículo 14",
            "inicio de ejecución",
            "piscina de emergencia", "depósito de relave",
            "botadero de estériles", "planta de procesos",
        ],
    },

    # -------------------------
    # NUEVO: ÁREA DE INFLUENCIA
    # -------------------------
    "AREA_INFLUENCIA": {
        "nombre": "Área de Influencia",
        "keywords": [
            "área de influencia", "area de influencia",
            "delimitación del área de influencia", "delimitacion del area de influencia",
            "justificación del área de influencia", "justificacion del area de influencia",
            "criterios de delimitación", "criterios de delimitacion",
            "polígono de área de influencia", "poligono de area de influencia",
            "radio de influencia",
            "componente en el área de influencia", "componente en el area de influencia",
            "área de estudio", "area de estudio",
        ],
    },

    "RESIDUOS": {
        "nombre": "Residuos Sólidos y Peligrosos",
        "keywords": [
            "residuo", "residuos",
            "rsd", "residuos sólidos domiciliarios",
            "rsinp", "residuos sólidos industriales no peligrosos",
            "respel", "residuos peligrosos",
            "reas", "residuos de establecimientos de atención de salud",
            "riles", "residuos industriales líquidos",
            "plan de manejo de residuos", "manifiesto de declaración",
            "sidrep", "sinader",
            "disposición final", "valorización", "reciclaje",
            "relleno sanitario", "relleno de seguridad",
            "bodega respel",
            "toxicidad", "inflamabilidad", "corrosividad", "reactividad",
            "ds 148/2003", "ds 148",
            "ley 20.920", "ley rep",
            "ds 189/2005", "ds 594/1999", "ds 594",
            "pas 138", "pas 139", "pas 140", "pas 141",
            "pas 142", "pas 143", "pas 144", "pas 145",
            "pas 126",
            "artículo 138", "artículo 139", "artículo 140",
            "artículo 141", "artículo 142", "artículo 143",
            "artículo 144", "artículo 145",
        ],
    },

    "SUSTANCIAS_PELIGROSAS": {
        "nombre": "Sustancias Peligrosas",
        "keywords": [
            "sustancias peligrosas", "suspel",
            "hoja de datos de seguridad", "hds",
            "nch 382", "9 clases",
            "cubeto de contención", "contención secundaria",
            "compatibilidad química", "segregación",
            "bodega de sustancias peligrosas",
            "transporte de cargas peligrosas",
            "ds 43/2015", "ds 78/2009",
            "nch 2190", "nch 2120",
            "ds 298/1994",
        ],
    },

    "TRANSPORTE_VIALIDAD": {
        "nombre": "Transporte y Vialidad",
        "keywords": [
            "transporte", "vialidad",
            "impacto vial", "estudio de impacto vial",
            "imiv", "eistu",
            "generación de viajes", "atracción de viajes",
            "nivel de servicio", "capacidad vial",
            "flujo vehicular", "flujos vehiculares",
            "intersección", "intersecciones",
            "sectra", "uoct", "seremitt",
            "saturn", "transyt", "aimsun", "estraus",
            "vivaldi", "modem", "modec",
            "libre circulación", "conectividad",
            "tiempos de desplazamiento",
            "ley 20.958", "ds 30/2017",
            "ds 83/1985", "dfl 850",
            "ley de caminos",
            "dirección de vialidad",
        ],
    },

    "PLAN_MEDIDAS": {
        "nombre": "Plan de Medidas de Mitigación, Reparación y Compensación",
        "keywords": [
            "plan de medidas", "medida de mitigación",
            "medidas de mitigación", "medida de reparación",
            "medidas de reparación", "medida de compensación",
            "medidas de compensación",
            "compromiso ambiental voluntario",
            "compensación de biodiversidad",
            "plan de rescate", "plan de revegetación",
            "plan de reforestación",
            "equivalencia ecológica", "adicionalidad",
            "no pérdida neta", "permanencia",
            "artículo 97", "artículo 98", "artículo 99",
            "artículo 100", "artículo 101", "artículo 102",
        ],
    },

    "PLAN_CONTINGENCIAS": {
        "nombre": "Plan de Contingencias y Emergencias",
        "keywords": [
            "plan de contingencia", "plan de emergencia",
            "contingencias y emergencias",
            "prevención de contingencias",
            "riesgo ambiental", "riesgos ambientales",
            "derrame", "incendio", "explosión", "fuga",
            "protocolo de actuación", "simulacro",
            "sistema de alerta temprana",
            "matriz de riesgos",
            "artículo 103", "artículo 104",
        ],
    },

    "PLAN_SEGUIMIENTO": {
        "nombre": "Plan de Seguimiento de Variables Ambientales",
        "keywords": [
            "plan de seguimiento", "seguimiento ambiental",
            "monitoreo ambiental", "variable de seguimiento",
            "frecuencia de muestreo", "punto de monitoreo",
            "puntos de monitoreo",
            "umbrales de acción",
            "informe de seguimiento",
            "monitoreo participativo",
            "verificación de cumplimiento",
            "efectividad de medidas",
            "artículo 105",
        ],
    },

    "NORMATIVA_AMBIENTAL": {
        "nombre": "Legislación y Normativa Ambiental Aplicable",
        "keywords": [
            "normativa ambiental aplicable",
            "legislación ambiental",
            "norma de emisión", "normas de emisión",
            "norma de calidad", "normas de calidad",
            "norma primaria", "norma secundaria",
            "plan de cumplimiento",
            "norma de referencia", "normas de referencia",
        ],
    },

    "PARTICIPACION_CIUDADANA": {
        "nombre": "Participación Ciudadana y Consulta Indígena",
        "keywords": [
            "participación ciudadana", "pac",
            "observaciones ciudadanas",
            "respuesta a observaciones",
            "consulta indígena",
            "convenio 169", "oit",
            "participación ciudadana temprana",
            "ponderación de observaciones",
        ],
    },

    "CAMBIO_CLIMATICO": {
        "nombre": "Cambio Climático y Gases de Efecto Invernadero",
        "keywords": [
            "cambio climático",
            "gases de efecto invernadero", "gei",
            "co2", "co₂", "ch4", "ch₄", "n2o", "n₂o",
            "huella de carbono",
            "carbono negro", "forzantes climáticos",
            "adaptación al cambio climático",
            "vulnerabilidad climática",
            "escenario rcp", "escenario ssp",
            "ley 21.455", "ley marco cambio climático",
            "ndc chile",
        ],
    },

    "RIESGO_SALUD": {
        "nombre": "Riesgo para la Salud de la Población",
        "keywords": [
            "riesgo para la salud", "riesgo en salud",
            "evaluación de riesgo en salud",
            "vía de exposición", "vías de exposición",
            "inhalación", "ingestión", "contacto dérmico",
            "población susceptible", "poblaciones susceptibles",
            "radiación electromagnética",
            "vectores sanitarios",
            "epidemiología",
            "artículo 5 rseia",
        ],
    },

    "MINERIA": {
        "nombre": "Aspectos Mineros",
        "keywords": [
            "minería", "minero", "minera",
            "depósito de relave", "relaves", "relave",
            "relave en pasta", "relave espesado",
            "botadero de estériles", "estériles",
            "plan de cierre de faena", "cierre de faena minera",
            "drenaje ácido", "dam", "aguas ácidas",
            "mineral", "concentrado", "lixiviación",
            "chancado", "molienda", "flotación",
            "pila de lixiviación",
            "pas 135", "pas 136", "pas 137",
            "pas 121", "pas 122", "pas 125", "pas 154",
            "artículo 135", "artículo 136", "artículo 137",
            "sernageomin",
        ],
    },

    # -------------------------
    # NUEVO: PAS
    # -------------------------
    "PAS": {
        "nombre": "PAS (Permisos Ambientales Sectoriales)",
        "keywords": [
            "pas", "permiso ambiental sectorial", "permisos ambientales sectoriales",
            # Artículos (variantes)
            "artículo 111", "articulo 111", "art. 111",
            "artículo 112", "articulo 112", "art. 112",
            "artículo 113", "articulo 113", "art. 113",
            "artículo 114", "articulo 114", "art. 114",
            "artículo 115", "articulo 115", "art. 115",
            "artículo 116", "articulo 116", "art. 116",
            "artículo 117", "articulo 117", "art. 117",
            "artículo 118", "articulo 118", "art. 118",
            "artículo 119", "articulo 119", "art. 119",
            "artículo 120", "articulo 120", "art. 120",
            "artículo 121", "articulo 121", "art. 121",
            "artículo 122", "articulo 122", "art. 122",
            "artículo 123", "articulo 123", "art. 123",
            "artículo 124", "articulo 124", "art. 124",
            "artículo 125", "articulo 125", "art. 125",
            "artículo 126", "articulo 126", "art. 126",
            "artículo 127", "articulo 127", "art. 127",
            "artículo 128", "articulo 128", "art. 128",
            "artículo 129", "articulo 129", "art. 129",
            "artículo 130", "articulo 130", "art. 130",
            "artículo 131", "articulo 131", "art. 131",
            "artículo 132", "articulo 132", "art. 132",
            "artículo 133", "articulo 133", "art. 133",
            "artículo 134", "articulo 134", "art. 134",
            "artículo 135", "articulo 135", "art. 135",
            "artículo 136", "articulo 136", "art. 136",
            "artículo 137", "articulo 137", "art. 137",
            "artículo 138", "articulo 138", "art. 138",
            "artículo 139", "articulo 139", "art. 139",
            "artículo 140", "articulo 140", "art. 140",
            "artículo 141", "articulo 141", "art. 141",
            "artículo 142", "articulo 142", "art. 142",
            "artículo 143", "articulo 143", "art. 143",
            "artículo 144", "articulo 144", "art. 144",
            "artículo 145", "articulo 145", "art. 145",
            "artículo 146", "articulo 146", "art. 146",
            "artículo 147", "articulo 147", "art. 147",
            "artículo 148", "articulo 148", "art. 148",
            "artículo 149", "articulo 149", "art. 149",
            "artículo 150", "articulo 150", "art. 150",
            "artículo 151", "articulo 151", "art. 151",
            "artículo 152", "articulo 152", "art. 152",
            "artículo 153", "articulo 153", "art. 153",
            "artículo 154", "articulo 154", "art. 154",
            "artículo 155", "articulo 155", "art. 155",
            "artículo 156", "articulo 156", "art. 156",
            "artículo 157", "articulo 157", "art. 157",
            "artículo 158", "articulo 158", "art. 158",
            "artículo 159", "articulo 159", "art. 159",
            "artículo 160", "articulo 160", "art. 160",
            "artículo 161", "articulo 161", "art. 161",
            # PAS n
            "pas 111", "pas 112", "pas 113", "pas 114", "pas 115", "pas 116",
            "pas 117", "pas 118", "pas 119", "pas 120", "pas 121",
            "pas 122", "pas 123", "pas 124", "pas 125", "pas 126",
            "pas 127", "pas 128", "pas 129", "pas 130",
            "pas 131", "pas 132", "pas 133", "pas 134",
            "pas 135", "pas 136", "pas 137",
            "pas 138", "pas 139", "pas 140", "pas 141", "pas 142", "pas 143",
            "pas 144", "pas 145", "pas 146", "pas 147", "pas 148", "pas 149",
            "pas 150", "pas 151", "pas 152", "pas 153", "pas 154",
            "pas 155", "pas 156", "pas 157", "pas 158", "pas 159",
            "pas 160", "pas 161",
        ],
    },
}

# =============================================================================
# NORMALIZACIÓN DE TEXTO
# =============================================================================
def normalizar(texto: str) -> str:
    """Normaliza texto para matching: minúsculas, espacios."""
    if not texto:
        return ""
    t = texto.lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t


def normalizar_sin_acentos(texto: str) -> str:
    """Normalización sin acentos para matching más flexible."""
    t = normalizar(texto)
    repl = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n"}
    for old, new in repl.items():
        t = t.replace(old, new)
    return t


# =============================================================================
# MOTOR DE CLASIFICACIÓN
# =============================================================================
def calcular_score_tema(
    tema_id: str,
    tema_data: dict,
    cap_norm: str,
    bis_norm: str,
    txt_norm: str,
    cap_sin: str,
    bis_sin: str,
    txt_sin: str,
) -> dict:
    """
    Calcula el score de un tema para una pregunta.
    Retorna: {"score": float, "matches": [str], "detalle": {...}}
    """
    keywords = tema_data["keywords"]
    matches = []
    score = 0.0
    detalle = {"capitulo": [], "bisagra": [], "texto": []}

    for kw in keywords:
        kw_norm = normalizar(kw)
        kw_sin = normalizar_sin_acentos(kw)

        # Keywords cortos: palabra completa
        if len(kw_norm) <= 4:
            pattern = r"\b" + re.escape(kw_norm) + r"\b"
            pattern_sin = r"\b" + re.escape(kw_sin) + r"\b"
        else:
            pattern = re.escape(kw_norm)
            pattern_sin = re.escape(kw_sin)

        found = False

        if cap_norm and (re.search(pattern, cap_norm) or re.search(pattern_sin, cap_sin)):
            score += PESO_CAPITULO
            detalle["capitulo"].append(kw)
            found = True

        if bis_norm and (re.search(pattern, bis_norm) or re.search(pattern_sin, bis_sin)):
            score += PESO_BISAGRA
            detalle["bisagra"].append(kw)
            found = True

        if txt_norm and (re.search(pattern, txt_norm) or re.search(pattern_sin, txt_sin)):
            score += PESO_TEXTO
            detalle["texto"].append(kw)
            found = True

        if found:
            matches.append(kw)

    return {"score": score, "matches": matches, "detalle": detalle}


def clasificar_pregunta(pregunta: dict) -> dict:
    """
    Clasifica una pregunta en temas ICSARA.
    Retorna:
      - tema_principal / tema_principal_id (compatibilidad: primer principal)
      - temas_principales / temas_principales_id (lista)
      - temas (lista completa ordenada con detalle)
    """
    cap = pregunta.get("capitulo", "") or ""
    bis = pregunta.get("bisagra", "") or ""
    txt = pregunta.get("texto", "") or ""

    cap_norm = normalizar(cap)
    bis_norm = normalizar(bis)
    txt_norm = normalizar(txt)

    cap_sin = normalizar_sin_acentos(cap)
    bis_sin = normalizar_sin_acentos(bis)
    txt_sin = normalizar_sin_acentos(txt)

    resultados = []
    for tema_id, tema_data in TAXONOMIA.items():
        r = calcular_score_tema(
            tema_id, tema_data,
            cap_norm, bis_norm, txt_norm,
            cap_sin, bis_sin, txt_sin,
        )
        if r["score"] >= MIN_SCORE:
            resultados.append({
                "id": tema_id,
                "nombre": tema_data["nombre"],
                "score": r["score"],
                "matches": r["matches"],
                "detalle": r["detalle"],
            })

    resultados.sort(key=lambda x: x["score"], reverse=True)

    if not resultados:
        return {
            "tema_principal": "Sin clasificar",
            "tema_principal_id": "SIN_CLASIFICAR",
            "temas_principales": [],
            "temas_principales_id": [],
            "temas": [],
        }

    top_score = resultados[0]["score"]
    thr_principal = max(MIN_SCORE, MULTI_PRINCIPAL_RATIO * top_score)

    principales = [t for t in resultados if t["score"] >= thr_principal]

    return {
        "tema_principal": principales[0]["nombre"],
        "tema_principal_id": principales[0]["id"],
        "temas_principales": [t["nombre"] for t in principales],
        "temas_principales_id": [t["id"] for t in principales],
        "temas": resultados,
    }


# =============================================================================
# MAIN
# =============================================================================

from app.pipeline.types import ClassificationSummary

def run_classification(preguntas_json_path: Path | str, out_dir: Path | str) -> ClassificationSummary:
    input_path = Path(preguntas_json_path)
    out_dir = Path(out_dir)
    output_path = out_dir / "preguntas_clasificadas.json"
    output_detalle_path = out_dir / "preguntas_clasificadas_detalle.json"

    if not input_path.exists():
        raise FileNotFoundError(f"preguntas.json not found: {input_path}")

    preguntas = json.loads(input_path.read_text(encoding="utf-8"))

    resultados_out = []
    resultados_detalle_out = []
    for p in preguntas:
        clf = clasificar_pregunta(p)
        top_matches = clf["temas"][0]["matches"][:10] if clf["temas"] else []

        principales_set = set(clf.get("temas_principales", []))
        secundarios = [t for t in clf.get("temas", []) if t["nombre"] not in principales_set]

        resultados_out.append({
            "numero": p.get("numero"),
            "capitulo": p.get("capitulo", ""),
            "bisagra": p.get("bisagra"),
            "tema_principal": clf["tema_principal"],
            "tema_principal_id": clf["tema_principal_id"],
            "temas_principales": clf.get("temas_principales", []),
            "temas_principales_id": clf.get("temas_principales_id", []),
            "score": clf["temas"][0]["score"] if clf["temas"] else 0,
            "temas_secundarios": [
                {"nombre": t["nombre"], "score": t["score"]}
                for t in secundarios[:3]
            ],
            "keywords_match": top_matches,
            "texto": p.get("texto", ""),
            "tablas_figuras": p.get("tablas_figuras", []),
        })

        resultados_detalle_out.append({
            "numero": p.get("numero"),
            "capitulo": p.get("capitulo", ""),
            "bisagra": p.get("bisagra"),
            "tema_principal": clf["tema_principal"],
            "tema_principal_id": clf["tema_principal_id"],
            "temas_principales": clf.get("temas_principales", []),
            "temas_principales_id": clf.get("temas_principales_id", []),
            "temas": clf.get("temas", []),
        })

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(resultados_out, ensure_ascii=False, indent=2), encoding="utf-8")

    output_detalle_path.parent.mkdir(parents=True, exist_ok=True)
    output_detalle_path.write_text(
        json.dumps(resultados_detalle_out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dist = Counter(r["tema_principal"] for r in resultados_out)
    n_sin = dist.get("Sin clasificar", 0)
    total = len(resultados_out)

    return ClassificationSummary(
        total=total,
        classified=total - n_sin,
        unclassified=n_sin,
        output_json=output_path,
        output_detail_json=output_detalle_path,
    )


def main() -> None:
    if not INPUT_JSON.exists():
        print(f"No se encontro: {INPUT_JSON}")
        return

    summary = run_classification(INPUT_JSON, DEFAULT_OUT_DIR)
    print(f"Salida: {summary.output_json}")
    print(f"Salida detalle: {summary.output_detail_json}")
    print(f"Clasificadas: {summary.classified}/{summary.total}")
    if summary.unclassified:
        print(f"Sin clasificar: {summary.unclassified}")


if __name__ == "__main__":
    main()
