from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
DEFAULT_FILES = [
    ROOT_DIR / "Pluto - All.csv",
    ROOT_DIR / "Translation_File.csv",
]

MISSING_KEY_PATTERN = re.compile(r"^<!MissingKey:\d+:(.*)>$", re.DOTALL)


def limpar_chave(valor: str) -> str:
    texto = "" if valor is None else str(valor)
    correspondencia = MISSING_KEY_PATTERN.match(texto)
    if not correspondencia:
        return texto
    return correspondencia.group(1).strip()


def processar_arquivo(caminho_entrada: Path) -> Path | None:
    if not caminho_entrada.exists():
        print(f"[Aviso] Arquivo nao encontrado: {caminho_entrada}")
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    caminho_saida = OUTPUT_DIR / caminho_entrada.name

    with caminho_entrada.open("r", encoding="utf-8-sig", newline="") as arquivo_entrada:
        leitor = csv.reader(arquivo_entrada)
        linhas = list(leitor)

    if not linhas:
        print(f"[Aviso] Arquivo vazio: {caminho_entrada.name}")
        return None

    cabecalho = linhas[0]
    linhas_processadas = [cabecalho]
    alteracoes = 0

    for linha in linhas[1:]:
        if not linha:
            linhas_processadas.append(linha)
            continue

        nova_linha = list(linha)
        chave_original = nova_linha[0]
        chave_limpa = limpar_chave(chave_original)
        if chave_limpa != chave_original:
            alteracoes += 1
            nova_linha[0] = chave_limpa
        linhas_processadas.append(nova_linha)

    with caminho_saida.open("w", encoding="utf-8-sig", newline="") as arquivo_saida:
        escritor = csv.writer(arquivo_saida, quoting=csv.QUOTE_MINIMAL)
        escritor.writerows(linhas_processadas)

    print(f"[OK] {caminho_entrada.name}: {alteracoes} chave(s) limpa(s) -> {caminho_saida}")
    return caminho_saida


def main() -> int:
    if len(sys.argv) > 1:
        arquivos = [Path(arg) for arg in sys.argv[1:]]
    else:
        arquivos = DEFAULT_FILES

    processados = 0
    for arquivo in arquivos:
        resultado = processar_arquivo(arquivo)
        if resultado is not None:
            processados += 1

    print(f"[Fim] Arquivos processados: {processados}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
