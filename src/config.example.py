"""
config.example.py - modelo de configuração da sincronização com a nuvem.

Como usar:
    cp config.example.py config.py
    # edite o config.py com os seus valores

O `config.py` está no .gitignore: NUNCA suba ele (nem as credenciais) pro GitHub.
Sem `config.py`, o sistema detecta e registra localmente normalmente;
só o envio pro Google Sheets/Drive fica desligado.
"""

import os

# Caminho dos arquivos de credenciais (ficam FORA do repositório)
_PASTA_HOME = os.path.expanduser("~")
SERVICE_ACCOUNT_PATH = os.path.join(_PASTA_HOME, "credentials.json")        # Sheets (Service Account)
OAUTH_CLIENT_SECRET_PATH = os.path.join(_PASTA_HOME, "oauth_credentials.json")  # Drive (OAuth)
OAUTH_TOKEN_PATH = os.path.join(_PASTA_HOME, "token.json")                  # gerado na 1a autorização

# ID da planilha (trecho da URL entre /d/ e /edit)
SPREADSHEET_ID = "COLE_AQUI_O_ID_DA_PLANILHA"

# ID da pasta do Drive onde as fotos serão salvas (último trecho da URL da pasta)
DRIVE_FOLDER_ID = "COLE_AQUI_O_ID_DA_PASTA_DO_DRIVE"
