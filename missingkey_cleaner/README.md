# Missing Key Cleaner

Este utilitário remove o wrapper `<!MissingKey:...: ...>` da primeira coluna dos CSVs do Pluto.

## Uso

Rodar sem argumentos processa os dois arquivos principais do projeto:

```powershell
python .\missingkey_cleaner\clean_missing_keys.py
```

Também aceita arquivos específicos:

```powershell
python .\missingkey_cleaner\clean_missing_keys.py .\Pluto - All.csv .\Translation_File.csv
```

## Saida

Os CSVs limpos sao gravados em `missingkey_cleaner\output\` com o mesmo nome do arquivo original.

## O que ele faz

- Lê a primeira coluna do CSV
- Se a célula começar com `<!MissingKey:N:` e terminar com `>`, remove esse wrapper
- Mantém o restante da linha intacto
