import json
import os
import re
import sys
import time

import pandas as pd
import requests

# ============================================================
# API key
# ============================================================
CHAVE_API = os.environ.get("GEMINI_API_KEY")
if not CHAVE_API:
    print("ERRO: variavel de ambiente GEMINI_API_KEY nao definida.")
    sys.exit(1)

MODELOS = [
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
]


def montar_url(modelo):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"


HEADERS = {
    "Content-Type": "application/json",
    "x-goog-api-key": CHAVE_API,
}

INSTRUCOES_SISTEMA = """
Voce e um tradutor profissional de localizacao de jogos de videogame.
Traduza os textos fornecidos do ingles para o Portugues do Brasil (PT-BR).
Regras de ouro:
1. Adapte nomes proprios de fantasia para soar bem em PT-BR.
2. O texto pode conter marcadores no formato §0§, §1§, §2§ etc. Eles representam codigo tecnico do jogo e sao inviolaveis: nunca traduza, altere, remova, adicione espaco, ou mude o numero dentro deles. Copie cada marcador exatamente como esta, apenas reposicionando-o naturalmente na frase em portugues se a gramatica exigir.
3. Quando receber uma lista de textos, responda em JSON valido no formato {"translations":["..."]} com a mesma quantidade de itens e na mesma ordem.
4. Retorne apenas a traducao final. Sem aspas adicionais, sem explicacoes.
"""

# Ajustes
PAUSA_ENTRE_CHAMADAS = 5.0
SALVAR_A_CADA = 25
MAX_TENTATIVAS = 3
TIMEOUT_REQUISICAO = 60
BACKOFF_INICIAL = 8
MAX_ITENS_POR_LOTE = 8
MAX_CARACTERES_POR_LOTE = 4500

CACHE_TRADUCOES = {}
CACHE_ARQUIVO = "translation_cache.json"
ULTIMA_REQUISICAO_TS = 0.0

# Protecao de tags/codigos do jogo
PADRAO_TAG = re.compile(r"(\{[^{}]*\}|%[sd])")


def proteger_tags(texto):
    tags_encontradas = PADRAO_TAG.findall(texto)
    texto_protegido = texto
    for indice, tag in enumerate(tags_encontradas):
        texto_protegido = texto_protegido.replace(tag, f"§{indice}§", 1)
    return texto_protegido, tags_encontradas


def restaurar_tags(texto_traduzido, tags_encontradas):
    resultado = texto_traduzido
    for indice, tag in enumerate(tags_encontradas):
        resultado = resultado.replace(f"§{indice}§", tag, 1)
    return resultado


def tags_intactas(texto_original, texto_traduzido):
    tags_orig = PADRAO_TAG.findall(str(texto_original))
    if not tags_orig:
        return True

    restante = str(texto_traduzido)
    for tag in tags_orig:
        if tag in restante:
            restante = restante.replace(tag, "", 1)
        else:
            return False
    return True


def _limpar_texto_resposta(texto):
    texto = str(texto).strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s*```$", "", texto)
    return texto.strip()


def _extrair_json_resposta(texto):
    texto = _limpar_texto_resposta(texto)
    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio == -1 or fim == -1 or fim <= inicio:
        return None
    trecho = texto[inicio : fim + 1]
    try:
        return json.loads(trecho)
    except json.JSONDecodeError:
        return None


def _extrair_detalhes_429(texto_resposta):
    try:
        dados = json.loads(texto_resposta)
    except json.JSONDecodeError:
        return None, None

    erro = dados.get("error", {}) if isinstance(dados, dict) else {}
    detalhes = erro.get("details", []) if isinstance(erro, dict) else []

    for item in detalhes:
        if not isinstance(item, dict):
            continue
        quota_id = item.get("quotaId")
        quota_metric = item.get("quotaMetric")
        retry_delay = item.get("retryDelay")

        if quota_id or quota_metric or retry_delay:
            return {
                "quota_id": quota_id,
                "quota_metric": quota_metric,
                "retry_delay": retry_delay,
            }, dados

    return None, dados


def _registrar_cache(texto_original, traducao):
    if pd.isna(texto_original):
        return
    if traducao is None:
        return

    texto_original = str(texto_original)
    traducao = str(traducao).strip()
    if traducao and tags_intactas(texto_original, traducao):
        CACHE_TRADUCOES[texto_original] = traducao


def _aguardar_limite_global():
    global ULTIMA_REQUISICAO_TS

    agora = time.monotonic()
    decorrido = agora - ULTIMA_REQUISICAO_TS
    if decorrido < PAUSA_ENTRE_CHAMADAS:
        time.sleep(PAUSA_ENTRE_CHAMADAS - decorrido)
    ULTIMA_REQUISICAO_TS = time.monotonic()


def carregar_cache_disco():
    if not os.path.exists(CACHE_ARQUIVO):
        return 0

    try:
        with open(CACHE_ARQUIVO, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except (OSError, json.JSONDecodeError):
        return 0

    if not isinstance(dados, dict):
        return 0

    carregadas = 0
    for texto_original, traducao in dados.items():
        if isinstance(texto_original, str) and isinstance(traducao, str) and traducao.strip():
            CACHE_TRADUCOES[texto_original] = traducao
            carregadas += 1
    return carregadas


def salvar_cache_disco():
    if not CACHE_TRADUCOES:
        return

    arquivo_tmp = f"{CACHE_ARQUIVO}.tmp"
    try:
        with open(arquivo_tmp, "w", encoding="utf-8") as arquivo:
            json.dump(CACHE_TRADUCOES, arquivo, ensure_ascii=False, indent=2)
        os.replace(arquivo_tmp, CACHE_ARQUIVO)
    except OSError as erro:
        print(f"\n[Aviso] Nao foi possivel salvar o cache em disco: {erro}")
        try:
            if os.path.exists(arquivo_tmp):
                os.remove(arquivo_tmp)
        except OSError:
            pass


def _payload_lote(textos_protegidos):
    prompt = json.dumps({"items": textos_protegidos}, ensure_ascii=False)
    return {
        "systemInstruction": {"parts": [{"text": INSTRUCOES_SISTEMA}]},
        "contents": [
            {
                "parts": [
                    {
                        "text": (
                            "Traduza os itens abaixo do ingles para PT-BR.\n"
                            "Retorne somente um JSON valido no formato {\"translations\":[\"...\"]}.\n"
                            "A quantidade de traducoes precisa ser exatamente igual a quantidade de itens.\n"
                            "Cada traducao deve manter os marcadores §0§, §1§, §2§ etc. sem alteracao.\n"
                            f"Entrada JSON:\n{prompt}"
                        )
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 4096,
        },
    }


def _montar_lotes(itens):
    lotes = []
    lote_atual = []
    caracteres_atual = 0

    for item in itens:
        item_texto = str(item)
        tamanho_item = len(item_texto)

        if lote_atual and (
            len(lote_atual) >= MAX_ITENS_POR_LOTE
            or caracteres_atual + tamanho_item > MAX_CARACTERES_POR_LOTE
        ):
            lotes.append(lote_atual)
            lote_atual = []
            caracteres_atual = 0

        lote_atual.append(item)
        caracteres_atual += tamanho_item

    if lote_atual:
        lotes.append(lote_atual)

    return lotes


def _tentar_modelo(modelo, payload):
    url = montar_url(modelo)
    espera = BACKOFF_INICIAL

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            _aguardar_limite_global()
            resposta = requests.post(
                url,
                headers=HEADERS,
                json=payload,
                timeout=TIMEOUT_REQUISICAO,
            )

            if resposta.status_code == 429 or resposta.status_code >= 500:
                detalhe = f" | Detalhe: {resposta.text}" if resposta.status_code == 429 else ""
                print(
                    f"\n[Aviso] {modelo}: status {resposta.status_code}, tentando de novo em {espera}s "
                    f"(tentativa {tentativa}/{MAX_TENTATIVAS}){detalhe}..."
                )

                if resposta.status_code == 429:
                    detalhes_429, _ = _extrair_detalhes_429(resposta.text)
                    if detalhes_429:
                        quota_id = detalhes_429.get("quota_id") or ""
                        quota_metric = detalhes_429.get("quota_metric") or ""
                        retry_delay = detalhes_429.get("retry_delay") or ""

                        if "GenerateRequestsPerDayPerProjectPerModel-FreeTier" in quota_id or \
                           "generativelanguage.googleapis.com/generate_content_free_tier_requests" in quota_metric:
                            print(
                                "\n[Quota diária atingida] Esse limite é por projeto/modelo, "
                                "nao por chave. Trocar a API key do mesmo projeto nao resolve. "
                                "Voce precisa esperar a virada do dia (Pacific Time) ou usar um "
                                "projeto com billing/paid tier."
                            )
                            return "quota_daily", None

                        if retry_delay:
                            match = re.search(r"(\d+)", str(retry_delay))
                            if match:
                                espera = max(1, int(match.group(1)))

                time.sleep(espera)
                espera *= 2
                continue

            if resposta.status_code != 200:
                print(f"\n[Erro definitivo] {modelo}: status {resposta.status_code}: {resposta.text[:300]}")
                return False, None

            dados = resposta.json()
            candidatos = dados.get("candidates")
            if not candidatos:
                motivo = dados.get("promptFeedback", {}).get("blockReason", "desconhecido")
                print(f"\n[Aviso] {modelo}: resposta sem candidatos (motivo: {motivo}).")
                return False, None

            partes = candidatos[0].get("content", {}).get("parts")
            if not partes:
                print(f"\n[Aviso] {modelo}: resposta sem texto.")
                return False, None

            traducao = partes[0].get("text", "").strip()
            return True, traducao if traducao else None

        except requests.exceptions.RequestException as erro:
            print(
                f"\n[Erro de conexao] {modelo}: {erro} - tentando de novo em {espera}s "
                f"(tentativa {tentativa}/{MAX_TENTATIVAS})..."
            )
            time.sleep(espera)
            espera *= 2

    print(f"\n[Falha] {modelo} esgotou {MAX_TENTATIVAS} tentativas. Tentando proximo modelo, se houver...")
    return False, None


def traduzir_texto_unico(texto):
    if pd.isna(texto) or str(texto).strip() == "":
        return ""

    texto_original = str(texto)
    texto_protegido, tags = proteger_tags(texto_original)

    payload = {
        "systemInstruction": {"parts": [{"text": INSTRUCOES_SISTEMA}]},
        "contents": [{"parts": [{"text": texto_protegido}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048,
        },
    }

    for modelo in MODELOS:
        sucesso, resultado = _tentar_modelo(modelo, payload)
        if sucesso == "quota_daily":
            return None
        if sucesso and resultado:
            traducao_final = restaurar_tags(resultado, tags)
            if tags_intactas(texto_original, traducao_final):
                _registrar_cache(texto_original, traducao_final)
                return traducao_final
            print(f"\n[Aviso] Tags corrompidas na traducao, mantendo original: {texto_original[:80]}...")
            return None
        if sucesso:
            return None

    print("\n[Falha definitiva] Todos os modelos falharam para esta linha. Mantendo original.")
    return None


def traduzir_texto(texto):
    texto_original = "" if pd.isna(texto) else str(texto)
    if texto_original in CACHE_TRADUCOES:
        return CACHE_TRADUCOES[texto_original]

    traducao = traduzir_texto_unico(texto_original)
    return traducao if traducao is not None else texto_original


def traduzir_lote(textos):
    textos = ["" if pd.isna(texto) else str(texto) for texto in textos]
    if not textos:
        return []

    protegidos = []
    tags_por_item = []
    for texto in textos:
        protegido, tags = proteger_tags(texto)
        protegidos.append(protegido)
        tags_por_item.append(tags)

    payload = _payload_lote(protegidos)

    for modelo in MODELOS:
        sucesso, resultado = _tentar_modelo(modelo, payload)
        if sucesso == "quota_daily":
            return None
        if not sucesso or not resultado:
            continue

        dados = _extrair_json_resposta(resultado)
        if not dados:
            print(f"\n[Aviso] {modelo}: resposta de lote fora do JSON esperado.")
            continue

        traducoes = dados.get("translations")
        if not isinstance(traducoes, list) or len(traducoes) != len(textos):
            print(f"\n[Aviso] {modelo}: lote com quantidade inesperada de traducoes.")
            continue

        resultado_final = []
        for texto_original, tags, traducao in zip(textos, tags_por_item, traducoes):
            traducao = "" if traducao is None else str(traducao).strip()
            if not traducao:
                resultado_final.append(None)
                continue

            traducao_restaurada = restaurar_tags(traducao, tags)
            if tags_intactas(texto_original, traducao_restaurada):
                resultado_final.append(traducao_restaurada)
            else:
                resultado_final.append(None)

        return resultado_final

    return [None] * len(textos)


def traduzir_arquivo(nome_arquivo, coluna_origem):
    print(f"\nIniciando traducao do arquivo: {nome_arquivo}...")

    if not os.path.exists(nome_arquivo):
        print(f"Erro: arquivo {nome_arquivo} nao encontrado na pasta.")
        return False

    try:
        df = pd.read_csv(nome_arquivo, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(nome_arquivo, encoding="latin-1")

    if coluna_origem not in df.columns:
        print(
            f"Erro: coluna '{coluna_origem}' nao existe em {nome_arquivo}. "
            f"Colunas disponiveis: {list(df.columns)}"
        )
        return False

    nome_saida = nome_arquivo.replace(".csv", "_PTBR.csv")

    if os.path.exists(nome_saida):
        df_parcial = pd.read_csv(nome_saida, encoding="utf-8-sig")
        if "pt_BR" in df_parcial.columns and len(df_parcial) == len(df):
            df["pt_BR"] = df_parcial["pt_BR"]
            print("Progresso anterior encontrado - retomando de onde parou.")

            corrigidas = 0
            for index, linha in df.iterrows():
                original = linha[coluna_origem]
                traduzido = str(df.at[index, "pt_BR"]).strip()
                if traduzido and traduzido.lower() != "nan" and not pd.isna(original):
                    if tags_intactas(original, traduzido):
                        _registrar_cache(original, traduzido)
                    else:
                        df.at[index, "pt_BR"] = ""
                        corrigidas += 1
            if corrigidas:
                print(f"{corrigidas} linha(s) com tags corrompidas foram marcadas para retraducao.")
        else:
            df["pt_BR"] = ""
    else:
        df["pt_BR"] = ""

    texto_para_indices = {}
    ordem_textos = []

    for index, linha in df.iterrows():
        ja_traduzido = str(df.at[index, "pt_BR"]).strip()
        if ja_traduzido and ja_traduzido.lower() != "nan":
            continue

        texto_original = linha[coluna_origem]
        if pd.isna(texto_original) or str(texto_original).strip() == "":
            df.at[index, "pt_BR"] = ""
            continue

        texto_original = str(texto_original)
        if texto_original in CACHE_TRADUCOES:
            df.at[index, "pt_BR"] = CACHE_TRADUCOES[texto_original]
            continue

        if texto_original not in texto_para_indices:
            texto_para_indices[texto_original] = []
            ordem_textos.append(texto_original)
        texto_para_indices[texto_original].append(index)

    lotes = _montar_lotes(ordem_textos)
    processados = 0

    try:
        for numero_lote, lote in enumerate(lotes, start=1):
            print(f"Traduzindo lote {numero_lote}/{len(lotes)} com {len(lote)} itens...", end="\r")

            traducoes = traduzir_lote(lote)
            for texto_original, traducao in zip(lote, traducoes):
                pode_cache = True
                if not traducao:
                    traducao_fallback = traduzir_texto_unico(texto_original)
                    if traducao_fallback is None:
                        traducao = texto_original
                        pode_cache = False
                    else:
                        traducao = traducao_fallback

                if traducao:
                    if pode_cache:
                        CACHE_TRADUCOES[texto_original] = traducao
                    for index in texto_para_indices.get(texto_original, []):
                        df.at[index, "pt_BR"] = traducao

                processados += 1
                if processados % SALVAR_A_CADA == 0:
                    df.to_csv(nome_saida, index=False, encoding="utf-8-sig")
                    salvar_cache_disco()

            salvar_cache_disco()
    except KeyboardInterrupt:
        df.to_csv(nome_saida, index=False, encoding="utf-8-sig")
        salvar_cache_disco()
        print("\nInterrompido pelo usuario. Progresso salvo.")
        return False

    for texto_original, indices in texto_para_indices.items():
        traducao = CACHE_TRADUCOES.get(texto_original)
        if not traducao:
            continue
        for index in indices:
            df.at[index, "pt_BR"] = traducao

    df.to_csv(nome_saida, index=False, encoding="utf-8-sig")
    salvar_cache_disco()
    print(f"\nArquivo salvo com sucesso: {nome_saida}")
    return True


if __name__ == "__main__":
    print("=== INICIANDO LOCALIZACAO DO JOGO (CONEXAO DIRETA) ===")

    carregadas = carregar_cache_disco()
    if carregadas:
        print(f"Cache carregado do disco: {carregadas} traducoes.")

    if not traduzir_arquivo("Pluto - All.csv", "en"):
        salvar_cache_disco()
        sys.exit(0)

    if not traduzir_arquivo("Translation_File.csv", "English"):
        salvar_cache_disco()
        sys.exit(0)

    print("\nTraducao completa e variaveis preservadas!")
