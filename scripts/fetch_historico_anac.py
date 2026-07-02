import csv
import io
import os
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERRO CRÍTICO] SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórios.")
    sys.exit(1)

db = create_client(SUPABASE_URL, SUPABASE_KEY)
print(f"Supabase conectado: {SUPABASE_URL}")

airports_env = os.environ.get("AIRPORTS", "SBCA")
AIRPORTS = [a.strip().upper() for a in airports_env.split(",") if a.strip()]
LOTE = 500

BRT = timezone(timedelta(hours=-3))
hoje = datetime.now(BRT)

if os.environ.get("ANO_MES"):
    ano_mes = os.environ["ANO_MES"].strip()
else:
    primeiro_do_mes = hoje.replace(day=1)
    mes_anterior = primeiro_do_mes - timedelta(days=1)
    ano_mes = mes_anterior.strftime("%Y-%m")

ano, mes = ano_mes.split("-")

print(f"Período histórico: {ano_mes}")
print(f"Aeroportos filtrados: {', '.join(AIRPORTS)}")

# URL corrigida (item C): estrutura real confirmada em
# https://sistemas.anac.gov.br/dadosabertos/Voos%20e%20opera%C3%A7%C3%B5es%20a%C3%A9reas/Voo%20Regular%20Ativo%20%28VRA%29/<ano>/
# Dentro do ano existe uma PASTA POR MÊS em português, ex: "05 - Maio/"
# e dentro dela o arquivo usa o mês SEM zero à esquerda, ex: VRA_20265.csv (maio/2026)
MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}
mes_int = int(mes)
pasta_mes = quote(f"{mes_int:02d} - {MESES_PT[mes_int]}")
VRA_URL = f"https://sistemas.anac.gov.br/dadosabertos/Voos%20e%20opera%C3%A7%C3%B5es%20a%C3%A9reas/Voo%20Regular%20Ativo%20%28VRA%29/{ano}/VRA_{ano}{mes}.csv"

VRA_URL_ALT = f"https://www.gov.br/anac/pt-br/assuntos/dados-e-estatisticas/dados-estatisticos/arquivos/VRA{ano}{mes}.csv"

# Nomes de coluna confirmados no CSV real baixado de VRA_20265.csv
# (cabeçalho: "ICAO Empresa Aérea";"Número Voo";"Código Autorização (DI)";"Código Tipo Linha";
#  "ICAO Aeródromo Origem";"ICAO Aeródromo Destino";"Partida Prevista";"Partida Real";
#  "Chegada Prevista";"Chegada Real";"Situação Voo";"Código Justificativa")
COLS = {
    "empresa": ["ICAO Empresa Aérea", "EMPRESA (SIGLA)", "Empresa (Sigla)", "sg_empresa_icao"],
    "voo": ["Número Voo", "NÚMERO VOO", "Numero Voo", "nr_voo"],
    "origem": ["ICAO Aeródromo Origem", "ORIGEM", "Aeroporto Origem", "sg_icao_origem"],
    "destino": ["ICAO Aeródromo Destino", "DESTINO", "Aeroporto Destino", "sg_icao_destino"],
    "partida_prev": ["Partida Prevista", "PARTIDA PREVISTA", "dt_partida_prevista"],
    "partida_real": ["Partida Real", "PARTIDA REAL", "dt_partida_real"],
    "chegada_prev": ["Chegada Prevista", "CHEGADA PREVISTA", "dt_chegada_prevista"],
    "chegada_real": ["Chegada Real", "CHEGADA REAL", "dt_chegada_real"],
    "situacao": ["Situação Voo", "SITUAÇÃO DE VOO", "Situacao Voo", "situacao"],
    "motivo": ["Código Justificativa", "MOTIVO", "Motivo Alteracao", "motivo_alteracao"],
}

def get_col(row: dict, key: str) -> str:
    for nome in COLS.get(key, [key]):
        if nome in row:
            return (row[nome] or "").strip()
    return ""

def _parse_raw(dt_str: str) -> datetime | None:
    if not dt_str:
        return None
    s = dt_str.strip()
    if not s or s.lower() == "null":
        return None
    # o VRA traz nanossegundos (ex: "2026-05-29 01:00:00.100000000"),
    # mas strptime só aceita até 6 dígitos (microssegundos) em %f — trunca antes de parsear
    if "." in s:
        base, frac = s.split(".", 1)
        s = f"{base}.{frac[:6]}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def parse_dt_anac(dt_str: str) -> str | None:
    dt = _parse_raw(dt_str)
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None

def diff_minutos(partida_prev: str, partida_real: str) -> int | None:
    dp = _parse_raw(partida_prev)
    dr = _parse_raw(partida_real)
    if not dp or not dr:
        return None
    return int((dr - dp).total_seconds() / 60)

def baixar_vra() -> list[dict]:
    for url in [VRA_URL, VRA_URL_ALT]:
        print(f"\nGET {url}")
        try:
            r = requests.get(url, timeout=120)
            if r.status_code == 404:
                print(f"  Não encontrado (404) — tentando URL alternativa.")
                continue
            r.raise_for_status()
            try:
                texto = r.content.decode("utf-8-sig")
            except UnicodeDecodeError:
                texto = r.content.decode("latin-1", errors="replace")
            # a 1ª linha do arquivo é um metadado ("Atualizado em: AAAA-MM-DD"),
            # não faz parte do cabeçalho do CSV — precisa ser removida antes do DictReader
            linhas_texto = texto.splitlines()
            if linhas_texto and linhas_texto[0].strip().lower().startswith("atualizado"):
                linhas_texto = linhas_texto[1:]
            reader = csv.DictReader(linhas_texto, delimiter=";")
            registros = list(reader)
            print(f"  VRA carregado: {len(registros)} linhas brutas")
            return registros
        except Exception as e:
            print(f"  [ERRO] {e}")
    return []

def processar_vra(linhas: list[dict]) -> list[dict]:
    resultado = []
    for row in linhas:
        origem = get_col(row, "origem").upper()
        destino = get_col(row, "destino").upper()
        if origem not in AIRPORTS and destino not in AIRPORTS:
            continue

        empresa = get_col(row, "empresa")
        nr_voo = get_col(row, "voo")
        partida_prev = get_col(row, "partida_prev")
        partida_real = get_col(row, "partida_real")
        chegada_prev = get_col(row, "chegada_prev")
        chegada_real = get_col(row, "chegada_real")
        situacao = get_col(row, "situacao")
        motivo = get_col(row, "motivo")

        # não existe coluna de data separada no CSV real — a data do voo
        # é a data da Partida Prevista; se ela vier "null" (acontece em ~2% das linhas),
        # cai para Chegada Prevista, depois Partida Real, como fallback
        dt_ref_base = (
            _parse_raw(partida_prev)
            or _parse_raw(chegada_prev)
            or _parse_raw(partida_real)
        )
        dt_ref = dt_ref_base.date().isoformat() if dt_ref_base else None

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

    print(f"  Registros filtrados para os aeroportos configurados: {len(resultado)}")
    return resultado

linhas_vra = baixar_vra()
if not linhas_vra:
    print("\n[AVISO] VRA não disponível para o período. Encerrando.")
    sys.exit(0)

registros = processar_vra(linhas_vra)
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

# Remove duplicatas internas antes do envio
# (o mesmo voo pode aparecer mais de uma vez no CSV bruto do VRA)
def deduplicar(lista: list) -> list:
    seen = set()
    result = []
    for r in lista:
        key = (
            r.get("ano_mes"),
            r.get("icao_empresa"),
            r.get("nr_voo"),
            r.get("icao_origem"),
            r.get("icao_destino"),
            r.get("dt_referencia"),
        )
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result

antes = len(registros)
registros = deduplicar(registros)
removidos = antes - len(registros)
if removidos:
    print(f"  Deduplicação: {removidos} registro(s) duplicado(s) removido(s) antes do envio")
