#!/usr/bin/env python3
"""
Robô: exporta clientes do Conexa (Prol Office) e envia as 10 primeiras
linhas em JSON para o webhook.

Fluxo (mapeado no site real):
  1. GET  /index.php?r=site/login                  -> captura o campo oculto "token" (CSRF)
  2. POST /index.php?r=site/login                  -> LoginForm[username], LoginForm[password], token
  3. GET  /index.php?r=cliente/admin&show_all=1&export=excel  -> baixa o Excel (geração lenta!)
  4. Lê a planilha, pega as 10 primeiras linhas e faz POST JSON no webhook

Também consulta a API do CRM (getInformacoes) para converter os nomes de
Origem e Interesse de cada cliente nos respectivos IDs, incluídos no body
como "origem_id" e "interesse_id".

Variáveis de ambiente (definidas como Secrets no GitHub):
  CONEXA_USER, CONEXA_PASS, WEBHOOK_URL, CONEXA_API_TOKEN
"""

import io
import json
import os
import sys

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://proloffice.conexa.app"
LOGIN_URL = f"{BASE_URL}/index.php?r=site/login"
EXPORT_URL = f"{BASE_URL}/index.php?r=cliente/admin&show_all=1&export=excel"

USER = os.environ["CONEXA_USER"]
PASS = os.environ["CONEXA_PASS"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
API_TOKEN = os.environ["CONEXA_API_TOKEN"]
API_INFO_URL = f"{BASE_URL}/index.php?r=configuracoes/crmApi/getInformacoes&token={API_TOKEN}"

# A exportação de todos os clientes é gerada na hora pelo servidor e demora.
EXPORT_TIMEOUT = 600  # segundos
NUM_LINHAS = 10

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def login(session: requests.Session) -> None:
    """Loga no Conexa preenchendo o form da página (inclui token CSRF)."""
    resp = session.get(LOGIN_URL, timeout=60)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    form = None
    for f in soup.find_all("form"):
        if f.find("input", {"type": "password"}):
            form = f
            break
    if form is None:
        raise RuntimeError("Formulário de login não encontrado — layout do site mudou?")

    # Começa com todos os hidden (token CSRF etc.) e sobrescreve usuário/senha
    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "")
    data["LoginForm[username]"] = USER
    data["LoginForm[password]"] = PASS
    data["LoginForm[rememberMe]"] = "1"

    action = form.get("action") or "/index.php?r=site/login"
    resp = session.post(BASE_URL + action, data=data, timeout=60)
    resp.raise_for_status()

    # Se ainda houver campo de senha na resposta, o login falhou
    if BeautifulSoup(resp.text, "html.parser").find("input", {"type": "password"}):
        raise RuntimeError("Login falhou — verifique CONEXA_USER/CONEXA_PASS.")
    print("Login OK")


def baixar_excel(session: requests.Session) -> bytes:
    print("Baixando exportação de clientes (pode demorar alguns minutos)...")
    resp = session.get(EXPORT_URL, timeout=EXPORT_TIMEOUT)
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "")
    print(f"Download OK: {len(resp.content)} bytes (Content-Type: {ctype})")
    if b"<input" in resp.content[:5000] and b"password" in resp.content[:5000]:
        raise RuntimeError("Recebi a página de login em vez do Excel — sessão inválida.")
    return resp.content


def ler_planilha(conteudo: bytes):
    """Lê o arquivo exportado e retorna (colunas, linhas) das 10 primeiras linhas.

    Exportações 'excel' de sistemas Yii podem ser .xlsx, .xls ou uma tabela
    HTML disfarçada — tenta os três formatos.
    """
    import pandas as pd

    df = None
    for leitor in (
        lambda b: pd.read_excel(io.BytesIO(b), engine="openpyxl"),
        lambda b: pd.read_excel(io.BytesIO(b), engine="xlrd"),
        lambda b: pd.read_html(io.BytesIO(b))[0],
        lambda b: pd.read_csv(io.BytesIO(b), sep=None, engine="python"),
    ):
        try:
            df = leitor(conteudo)
            break
        except Exception:
            continue
    if df is None:
        raise RuntimeError("Não consegui ler o arquivo exportado em nenhum formato conhecido.")

    df = df.head(NUM_LINHAS)
    # NaN -> None para virar null no JSON
    df = df.astype(object).where(pd.notnull(df), None)
    colunas = [str(c) for c in df.columns]
    linhas = df.to_dict(orient="records")
    print(f"Planilha lida: {len(colunas)} colunas, enviando {len(linhas)} linhas")
    return colunas, linhas


import unicodedata


def _norm(txt) -> str:
    """normaliza: sem acentos, minúsculas, espaços únicos."""
    s = " ".join(str(txt).split()).casefold()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_STOPWORDS = {"de", "da", "do", "para", "e", "em", "a", "o"}


def _tokens(txt) -> set:
    return {t for t in _norm(txt).replace("/", " ").split() if t not in _STOPWORDS}


def match_fuzzy(valor, mapa_nome_id):
    """Casa um texto com a tabela nome->id: exato primeiro, depois por tokens.

    Ex.: Tag "Auditório" casa com "Aluguel de Auditório";
    "Sala de reunião para 06 pessoas" casa com "Aluguel de sala de reunião 06 pessoas".
    """
    if not valor:
        return None
    chave = _norm(valor)
    if chave in mapa_nome_id:
        return mapa_nome_id[chave]

    tv = _tokens(valor)
    if not tv:
        return None
    melhor, melhor_score = None, 0.0
    for nome, id_ in mapa_nome_id.items():
        tn = _tokens(nome)
        if not tn:
            continue
        inter = tv & tn
        # um lado precisa estar (quase) contido no outro
        score = len(inter) / min(len(tv), len(tn))
        if score >= 0.99 and len(inter) > melhor_score:
            melhor, melhor_score = id_, len(inter)
    return melhor


# De-para manual para valores de Origem que não existem na tabela do CRM.
# Ex.: DE_PARA_ORIGEM = {"cadastro publico": 13}
DE_PARA_ORIGEM = {}

# Quando não encontrar correspondência, usa o ID de "Outros" da própria tabela.
FALLBACK_OUTROS = True


def buscar_mapas_crm():
    """Consulta getInformacoes e retorna (origens, interesses) como nome->id.

    A resposta é uma lista de listas; o 3º item são as origens e o 6º os
    interesses, ambos no formato [{"id": "Nome", ...}].
    """
    resp = requests.get(API_INFO_URL, timeout=60)
    resp.raise_for_status()
    dados = json.loads(resp.text)

    def mapa(item):
        m = {}
        for bloco in item:
            for id_, nome in bloco.items():
                m[_norm(nome)] = int(id_)
        return m

    origens = mapa(dados[2])      # 3º item
    interesses = mapa(dados[5])   # 6º item
    print(f"CRM API OK: {len(origens)} origens, {len(interesses)} interesses")
    return origens, interesses


def anexar_ids(colunas, linhas, origens, interesses):
    """Adiciona origem_id e interesse_id a cada linha.

    Origem: coluna "Origem" (com de-para manual para valores fora da tabela).
    Interesse: coluna "Interesse" se existir; senão usa "Tags" (é onde o
    interesse aparece no export de clientes), com casamento aproximado.
    """
    col_origem = next((c for c in colunas if "origem" in _norm(c)), None)
    col_interesse = next(
        (c for c in colunas if "interesse" in _norm(c)),
        next((c for c in colunas if _norm(c) == "tags"), None),
    )

    for linha in linhas:
        v_origem = linha.get(col_origem) if col_origem else None
        v_interesse = linha.get(col_interesse) if col_interesse else None

        origem_id = origens.get(_norm(v_origem)) if v_origem else None
        if origem_id is None and v_origem:
            origem_id = DE_PARA_ORIGEM.get(_norm(v_origem))
        if origem_id is None and FALLBACK_OUTROS:
            origem_id = origens.get("outros")
        linha["origem_id"] = origem_id

        interesse_id = None
        if v_interesse:
            # Tags podem vir múltiplas separadas por ; ou , — usa a 1ª que casar
            for parte in str(v_interesse).replace(";", ",").split(","):
                interesse_id = match_fuzzy(parte.strip(), interesses)
                if interesse_id is not None:
                    break
        if interesse_id is None and FALLBACK_OUTROS:
            interesse_id = interesses.get("outros")
        linha["interesse_id"] = interesse_id
    return linhas


def enviar_webhook(colunas, linhas) -> None:
    payload = {
        "origem": "conexa-proloffice",
        "total_linhas_enviadas": len(linhas),
        "colunas": colunas,
        "clientes": linhas,
    }
    resp = requests.post(
        WEBHOOK_URL,
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=60,
    )
    resp.raise_for_status()
    print(f"Webhook OK: HTTP {resp.status_code}")


def main() -> int:
    session = requests.Session()
    session.headers.update({"User-Agent": UA})
    login(session)
    conteudo = baixar_excel(session)
    colunas, linhas = ler_planilha(conteudo)
    origens, interesses = buscar_mapas_crm()
    linhas = anexar_ids(colunas, linhas, origens, interesses)
    enviar_webhook(colunas, linhas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
