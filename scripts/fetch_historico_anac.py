"""
fetch_historico_anac.py — Dados históricos ANAC/VRA + Supabase
Busca o arquivo VRA (Voo Regular Ativo) do portal de dados abertos da ANAC,
processa e insere na tabela historico_vra do Supabase.

Execução: mensal (1º dia de cada mês via GitHub Actions)

Variáveis de ambiente:
  SUPABASE_URL         → URL do projeto (GitHub Secret)
  SUPABASE_SERVICE_KEY → secret key / service_role key (GitHub Secret)
  AIRPORTS             → ICAOs para filtrar (GitHub Variable)
  ANO_MES              → Período a buscar no formato AAAA-MM
                         Padrão: mês anterior ao atual
"""

import csv
import io
import os
import sys
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

# ── Credenciais ───────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERRO CRÍTICO] SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios.")
    sys.exit(1)

db = create_client(SUPABASE_URL, SUPABASE_KEY)
print(f"Supabase conectado: {SUPABASE_URL}")

# ── Configurações ─────────────────────────────────────────────────────────────

airports_env = os.environ.get("AIRPORTS", "SBCA")
AIRPORTS     = [a.strip().upper() for a in airports_env.split(",") if a.strip()]
LOTE         = 500

# Período: usa mês anterior por padrão (o VRA do mês atual fica disponível
# somente após o fechamento do mês)
BRT  = timezone(timedelta(hours=-3))
hoje = datetime.now(BRT)

if os.environ.get("ANO_MES"):
    ano_mes = os.environ["ANO_MES"].strip()  # ex: 2026-04
else:
    primeiro_do_mes = hoje.replace(day=1)
    mes_anterior    = primeiro_do_mes - timedelta(days=1)
    ano_mes         = mes_anterior.strftime("%Y-%m")

ano, mes = ano_mes.split("-")

print(f"Período histórico: {ano_mes}")
print(f"Aeroportos filtrados: {', '.join(AIRPORTS)}")

# ── URL do VRA ────────────────────────────────────────────────────────────────

VRA_URL = (
    f"https://siros.anac.gov.br/siros/registros/diversos/vra/"
    f"{ano}/VRA_{ano}_{mes}.csv"
)

# Mapeamento robusto de colunas – chave normalizada e alternativas com encoding
COLS = {
    "empresa": [
        "sigla icao empresa aerea",
        "sigla icao empresa aÃ©rea",
        "sigla icao empresa aérea"
    ],
    "voo": [
        "numero voo",
        "número voo",
        "nÃºmero voo"
    ],
    "origem": [
        "sigla icao aeroporto origem"
    ],
    "destino": [
        "sigla icao aeroporto destino"
    ],
    "dt_ref": [
        "referencia",
        "referência",
        "referÃªncia"
    ],
    "partida_prev": [
        "partida prevista"
    ],
    "partida_real": [
        "partida real"
    ],
    "chegada_prev": [
        "chegada prevista"
    ],
    "chegada_real": [
        "chegada real"
    ],
    "situacao": [
        "situacao voo",
        "situação voo",
        "situaÃ§Ã£o voo"
    ],
    "motivo": [
        "justificativa"
    ]
}


def _normalize_key(key: str) -> str:
    """Remove espaços, BOM e coloca em minúsculas para comparação exata."""
    return key.strip().lower().lstrip('\ufeff')


def get_col(row: dict, key: str) -> str:
    """Obtém valor da coluna usando mapeamento tolerante a encoding e espaços."""
    # Cria um dicionário normalizado da linha para buscas rápidas
    norm_row = {_normalize_key(k): v for k, v in row.items()}
    for nome in COLS.get(key, [key]):
        nn = _normalize_key(nome)
        if nn in norm_row:
            val = norm_row[nn]
            if isinstance(val, str):
                return val.strip()
            return str(val) if val is not None else ""
    return ""


def parse_dt_anac(dt_str: str) -> str | None:
    """Converte 'DD/MM/YYYY HH:MM' para ISO UTC."""
    if not dt_str or len(dt_str) < 16:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def diff_minutos(partida_prev: str, partida_real: str) -> int | None:
    """Calcula atraso em minutos entre horário previsto e real."""
    try:
        fmt = "%d/%m/%Y %H:%M"
        dp  = datetime.strptime(partida_prev.strip(), fmt)
        dr  = datetime.strptime(partida_real.strip(), fmt)
        return int((dr - dp).total_seconds() / 60)
    except Exception:
        return None


# ── Busca o arquivo VRA ───────────────────────────────────────────────────────

def baixar_vra() -> list[dict]:
    url = VRA_URL
    print(f"\nGET {url}")

    try:
        r = requests.get(url, timeout=120)
        print(f"Status Code: {r.status_code}")
        print(f"Content-Type: {r.headers.get('content-type')}")

        if r.status_code == 404:
            print("Não encontrado (404)")
            return []

        r.raise_for_status()

        # Tenta decodificar como UTF-8 primeiro; se falhar, usa latin-1
        try:
            texto = r.content.decode("utf-8")
        except UnicodeDecodeError:
            texto = r.content.decode("latin-1", errors="replace")

        print("\n===== INÍCIO DO ARQUIVO =====")
        print(texto[:1000])
        print("===== FIM DA AMOSTRA =====\n")

        reader = csv.DictReader(io.StringIO(texto), delimiter=";")

        # Remove BOM do primeiro nome de coluna, se presente
        if reader.fieldnames:
            reader.fieldnames = [
                fn.lstrip('\ufeff') if fn else fn
                for fn in reader.fieldnames
            ]

        print("Colunas encontradas (normalizadas):")
        for col in reader.fieldnames:
            print(f"  '{col}'")

        registros = list(reader)
        print(f"VRA carregado: {len(registros)} linhas brutas")

        if registros:
            print("\nPrimeiro registro (raw):")
            for k, v in registros[0].items():
                print(f"  {k}: {v}")

        return registros

    except Exception as e:
        print(f"[ERRO] {e}")
        return []


# ── Processa e filtra registros ───────────────────────────────────────────────

def processar_vra(linhas: list[dict]) -> list[dict]:
    resultado = []
    aeroportos_vistos = set()
    primeiro_que_passaria = None  # guarda o primeiro registro que passa no filtro

    for i, row in enumerate(linhas):
        origem = get_col(row, "origem").upper()
        destino = get_col(row, "destino").upper()

        # Coleciona para estatística
        if origem:
            aeroportos_vistos.add(origem)
        if destino:
            aeroportos_vistos.add(destino)

        # Debug pesado apenas nos primeiros registros
        if i < 10:
            print(f"\n--- DEBUG REGISTRO {i} ---")
            print(f"Origem : '{origem}'")
            print(f"Destino: '{destino}'")
            print(f"Passaria? {origem in AIRPORTS or destino in AIRPORTS}")

        # Verifica se passa no filtro
        if origem not in AIRPORTS and destino not in AIRPORTS:
            continue

        # Primeiro registro que passou
        if primeiro_que_passaria is None:
            primeiro_que_passaria = {
                "indice": i,
                "origem": origem,
                "destino": destino,
                "exemplo": row
            }

        empresa = get_col(row, "empresa")
        nr_voo = get_col(row, "voo")
        dt_ref_str = get_col(row, "dt_ref")
        partida_prev = get_col(row, "partida_prev")
        partida_real = get_col(row, "partida_real")
        chegada_prev = get_col(row, "chegada_prev")
        chegada_real = get_col(row, "chegada_real")
        situacao = get_col(row, "situacao")
        motivo = get_col(row, "motivo")

        dt_ref = None
        if dt_ref_str:
            try:
                for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
                    try:
                        dt_ref = datetime.strptime(dt_ref_str.strip(), fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        resultado.append({
            "ano_mes": ano_mes,
            "icao_empresa": empresa or None,
            "nr_voo": nr_voo or None,
            "icao_origem": origem or None,
            "icao_destino": destino or None,
            "dt_referencia": dt_ref,
            "partida_real": parse_dt_anac(partida_real),
            "chegada_real": parse_dt_anac(chegada_real),
            "atraso_partida": diff_minutos(partida_prev, partida_real),
            "atraso_chegada": diff_minutos(chegada_prev, chegada_real),
            "situacao": situacao.lower() if situacao else None,
            "motivo_alteracao": motivo or None,
        })

    print(f"\nTotal de aeroportos distintos encontrados: {len(aeroportos_vistos)}")
    if aeroportos_vistos:
        amostra = sorted(aeroportos_vistos)[:50]
        print(f"Amostra: {amostra}")

    if primeiro_que_passaria:
        print(f"\nPrimeiro registro que passaria no filtro (índice {primeiro_que_passaria['indice']}):")
        print(f"  Origem: {primeiro_que_passaria['origem']}")
        print(f"  Destino: {primeiro_que_passaria['destino']}")
    else:
        print("\nNENHUM registro passou no filtro. Verifique se os aeroportos desejados estão no arquivo.")

    print(f"\nRegistros filtrados: {len(resultado)}")
    return resultado


# ── Inserção no Supabase ──────────────────────────────────────────────────────

linhas_vra = baixar_vra()
if not linhas_vra:
    print("\n[AVISO] VRA não disponível para o período. Encerrando.")
    sys.exit(0)
#--- evita as duplicações
registros = processar_vra(linhas_vra)

registros_unicos = {}

for r in registros:
    chave = (
        r["ano_mes"],
        r["icao_empresa"],
        r["nr_voo"],
        r["icao_origem"],
        r["icao_destino"],
        r["dt_referencia"],
    )
    registros_unicos[chave] = r

registros = list(registros_unicos.values())

print(f"Registros após deduplicação: {len(registros)}")

processados = 0
erros = 0

for i in range(0, len(registros), LOTE):
    lote = registros[i:i + LOTE]
    num_lote = i // LOTE + 1
    try:
        db.table("historico_vra").upsert(
            lote,
            on_conflict="ano_mes,icao_empresa,nr_voo,icao_origem,icao_destino,dt_referencia",
        ).execute()
        processados += len(lote)
        print(f"  Lote {num_lote}: {len(lote)} registros enviados/processados")
    except Exception as e:
        erros += 1
        print(f"  [ERRO] Lote {num_lote}: {e}")

print(f"\nConcluído — {processados} registros históricos enviados/processados.")
if erros > 0:
    print(f"[ATENÇÃO] {erros} lote(s) com erro.")
    sys.exit(1)
else:
    sys.exit(0)
