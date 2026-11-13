import geopandas as gpd
import requests
import zipfile
import io
import os
import re
import unicodedata
import pandas as pd

from shapely.geometry import box

# ============================================================
# CONFIGURAÇÕES
# ============================================================

PASTA_DADOS = "dados"
ARQUIVO_SAIDA = "mapa.svg"
ANO_IBGE = 2025

ESTADOS = ["CE", "PE", "PB", "PI", "RN"]

CIDADES_CARIRI = [
    "Abaiara", "Altaneira", "Antonina do Norte", "Araripe", "Assaré",
    "Aurora", "Barbalha", "Barro", "Brejo Santo", "Campos Sales",
    "Caririaçu", "Crato", "Farias Brito", "Granjeiro", "Jardim",
    "Jati", "Juazeiro do Norte", "Lavras da Mangabeira", "Mauriti",
    "Milagres", "Missão Velha", "Nova Olinda", "Penaforte",
    "Porteiras", "Potengi", "Salitre", "Santana do Cariri",
    "Tarrafas", "Várzea Alegre"
]

# Incluindo Icó e Iguatu como alvos interativos
CIDADES_ALVO = CIDADES_CARIRI + ["Iguatu"]

# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar_nome(nome):
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return re.sub(r"[^a-zA-Z0-9]+", "_", nome).strip("_")

def criar_id(nome, estado):
    return f"{normalizar_nome(nome)}_{estado}"

def baixar_malha(estado):
    url = (f"https://geoftp.ibge.gov.br/organizacao_do_territorio/"
           f"malhas_territoriais/malhas_municipais/municipio_{ANO_IBGE}/"
           f"UFs/{estado}/{estado}_Municipios_{ANO_IBGE}.zip")
    print(f"Baixando malha de {estado}...")
    resposta = requests.get(url)
    resposta.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resposta.content)) as arquivo_zip:
        arquivo_zip.extractall(os.path.join(PASTA_DADOS, estado))

def encontrar_shapefile(pasta):
    for raiz, _, arquivos in os.walk(pasta):
        for arquivo in arquivos:
            if arquivo.lower().endswith(".shp"):
                return os.path.join(raiz, arquivo)
    raise FileNotFoundError(f"Nenhum arquivo .shp encontrado em {pasta}")

# ============================================================
# PREPARAÇÃO E LEITURA
# ============================================================

os.makedirs(PASTA_DADOS, exist_ok=True)

malhas = []
for estado in ESTADOS:
    pasta_estado = os.path.join(PASTA_DADOS, estado)
    if not os.path.exists(pasta_estado) or not any(a.lower().endswith(".shp") for _, _, arqs in os.walk(pasta_estado) for a in arqs):
        baixar_malha(estado)
    malhas.append(gpd.read_file(encontrar_shapefile(pasta_estado)))

municipios = gpd.GeoDataFrame(pd.concat(malhas, ignore_index=True), crs=malhas[0].crs)

# ============================================================
# PROJEÇÃO E CÁLCULO DE ESCALA (PIXELS EXATOS)
# ============================================================

# Projeta para metros (Web Mercator)
municipios_3857 = municipios.to_crs(epsg=3857)

# Simplifica *antes* de cortar para evitar os artefatos visuais
municipios_3857["geometry"] = municipios_3857.geometry.simplify(tolerance=20, preserve_topology=True)

# Identifica as bordas apenas dos municípios alvo
alvo_gdf = municipios_3857[
    (municipios_3857["NM_MUN"].isin(CIDADES_ALVO)) & 
    (municipios_3857["SIGLA_UF"] == "CE")
]

minx_alvo, miny_alvo, maxx_alvo, maxy_alvo = alvo_gdf.total_bounds

# Configuração exata das margens do SVG
largura_canvas_total = 1200
margem_x_px = 40
margem_y_px = 20

largura_interna = largura_canvas_total - (margem_x_px * 2)
escala = largura_interna / (maxx_alvo - minx_alvo)

altura_interna = (maxy_alvo - miny_alvo) * escala
altura_canvas_total = int(altura_interna + (margem_y_px * 2))

# ============================================================
# CLIPPING (CORTE DO MAPA)
# ============================================================

# Converte a margem de pixels desejada para metros e cria a Bounding Box
margem_x_metros = margem_x_px / escala
margem_y_metros = margem_y_px / escala

bbox = box(
    minx_alvo - margem_x_metros, 
    miny_alvo - margem_y_metros, 
    maxx_alvo + margem_x_metros, 
    maxy_alvo + margem_y_metros
)

# Corta as geometrias para encaixarem perfeitamente no ViewBox do SVG
municipios_clip = gpd.clip(municipios_3857, bbox)

# ============================================================
# SEPARAÇÃO: ALVOS vs FUNDO
# ============================================================

is_alvo = (municipios_clip["NM_MUN"].isin(CIDADES_ALVO)) & (municipios_clip["SIGLA_UF"] == "CE")

mapa_alvo = municipios_clip[is_alvo].copy()
mapa_fundo = municipios_clip[~is_alvo].copy()

# ============================================================
# CONVERSÃO PARA PATH SVG
# ============================================================

def converter_ponto(x, y):
    # Calcula a posição baseada no alvo, aplicando as margens exigidas
    svg_x = (x - minx_alvo) * escala + margem_x_px
    svg_y = (maxy_alvo - y) * escala + margem_y_px
    return svg_x, svg_y

def geometria_para_path(geometria):
    def obter_poligonos(geom):
        if geom.geom_type == "Polygon": return [geom]
        elif geom.geom_type == "MultiPolygon": return list(geom.geoms)
        elif geom.geom_type == "GeometryCollection":
            return [g for geom_part in geom.geoms for g in obter_poligonos(geom_part)]
        return []

    poligonos = obter_poligonos(geometria)
    if not poligonos: return ""

    path = ""
    for poligono in poligonos:
        exterior = list(poligono.exterior.coords)
        if not exterior: continue

        x, y = converter_ponto(*exterior[0])
        path += f"M {x:.2f},{y:.2f} "
        for ponto in exterior[1:]:
            x, y = converter_ponto(*ponto)
            path += f"L {x:.2f},{y:.2f} "
        path += "Z "

        for interior in poligono.interiors:
            coordenadas = list(interior.coords)
            if not coordenadas: continue
            x, y = converter_ponto(*coordenadas[0])
            path += f"M {x:.2f},{y:.2f} "
            for ponto in coordenadas[1:]:
                x, y = converter_ponto(*ponto)
                path += f"L {x:.2f},{y:.2f} "
            path += "Z "
    return path

# ============================================================
# GERAR PATHS
# ============================================================

paths_fundo = []
paths_alvo = []

# 1. Municípios de fundo (cinzas, com fronteiras, sem ID)
for _, municipio in mapa_fundo.iterrows():
    path_data = geometria_para_path(municipio.geometry)
    if path_data:
        paths_fundo.append(f'''
        <path
            class="municipio-fundo"
            d="{path_data}"
            fill="#f9f3f0"
            stroke="#e8d0c5"
            stroke-width="1"
            pointer-events="none"
        />
        ''')

# 2. Municípios interativos (brancos, com IDs próprios)
for _, municipio in mapa_alvo.iterrows():
    nome = municipio["NM_MUN"]
    estado = municipio["SIGLA_UF"]
    svg_id = criar_id(nome, estado)
    path_data = geometria_para_path(municipio.geometry)

    if path_data:
        paths_alvo.append(f'''
        <path
            id="{svg_id}"
            class="municipio-alvo"
            data-cidade="{nome}"
            data-estado="{estado}"
            d="{path_data}"
            fill="#eedcd3"
            stroke="#502C1D"
            stroke-width="1.5"
            style="cursor: pointer;"
        />
        ''')

# ============================================================
# SALVAR DOCUMENTO SVG
# ============================================================

svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg
    id="mapa-cariri"
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 {largura_canvas_total} {altura_canvas_total}"
>
    <defs>
        <!-- Filtro para destacar o Cariri do fundo -->
        <filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
            <feDropShadow dx="0" dy="2" stdDeviation="4" flood-opacity="0.15"/>
        </filter>
    </defs>
    
    <rect width="100%" height="100%" fill="#f9f3f0" />
    
    <g id="Municipios_Fundo">s
        {''.join(paths_fundo)}
    </g>
    
    <g id="Municipios_Alvo" filter="url(#shadow)">
        {''.join(paths_alvo)}
    </g>
</svg>
'''

with open(ARQUIVO_SAIDA, "w", encoding="utf-8") as arquivo:
    arquivo.write(svg)

print(f"SVG gerado com sucesso! Arquivo salvo como {ARQUIVO_SAIDA}.")