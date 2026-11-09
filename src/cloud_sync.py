"""
cloud_sync.py - Sincroniza as violações pendentes (SQLite) com Google Sheets + Drive.

Fluxo:
    registro.listar_pendentes() -> sobe foto pro Drive -> adiciona linha no Sheets
    -> registro.marcar_enviado(id)

Se não houver internet (ou o Google falhar), os registros continuam pendentes no
banco e são reenviados na próxima tentativa. Nada se perde.

Configuração: copie `config.example.py` para `config.py` e preencha com os seus
IDs e caminhos de credenciais. O `config.py` NÃO vai pro Git (veja o .gitignore).
Sem `config.py` a detecção local continua funcionando; só a nuvem fica desligada.

Uso manual (autorizar o Drive de novo / forçar uma sincronização):
    python3 cloud_sync.py
"""

import os

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

import registro

try:
    import config
except ImportError:
    config = None

_PASTA_HOME = os.path.expanduser("~")
SERVICE_ACCOUNT_PATH = getattr(
    config, "SERVICE_ACCOUNT_PATH", os.path.join(_PASTA_HOME, "credentials.json")
)
OAUTH_CLIENT_SECRET_PATH = getattr(
    config, "OAUTH_CLIENT_SECRET_PATH", os.path.join(_PASTA_HOME, "oauth_credentials.json")
)
OAUTH_TOKEN_PATH = getattr(
    config, "OAUTH_TOKEN_PATH", os.path.join(_PASTA_HOME, "token.json")
)

SPREADSHEET_ID = getattr(config, "SPREADSHEET_ID", "")
DRIVE_FOLDER_ID = getattr(config, "DRIVE_FOLDER_ID", "")

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Inicializados sob demanda (e não no import) pra o detect.py não quebrar
# se o Pi estiver sem internet quando for iniciado.
_sheet = None
_drive_service = None


# ============================================================
# AUTENTICAÇÃO
# ============================================================
def _obter_credenciais_drive(interativo=False):
    """
    Drive: autentica como o PRÓPRIO usuário (OAuth), porque Service Accounts
    não têm cota de armazenamento em contas pessoais ("storageQuotaExceeded").

    interativo=False (padrão, usado pela thread de fundo): só reaproveita/renova
    o token. Se não der, levanta erro em vez de abrir o navegador.
    interativo=True (só via `python3 cloud_sync.py`): abre o navegador no Pi
    pra autorizar de novo.
    """
    creds = None
    if os.path.exists(OAUTH_TOKEN_PATH):
        creds = UserCredentials.from_authorized_user_file(OAUTH_TOKEN_PATH, DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interativo:
            flow = InstalledAppFlow.from_client_secrets_file(
                OAUTH_CLIENT_SECRET_PATH, DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError(
                "Token do Drive ausente ou invalido. "
                "Rode `python3 cloud_sync.py` no Pi (com monitor) pra autorizar de novo."
            )
        with open(OAUTH_TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return creds


def _inicializar(interativo=False):
    """Conecta no Sheets (Service Account) e no Drive (OAuth), se ainda não estiver conectado."""
    global _sheet, _drive_service

    if not SPREADSHEET_ID or not DRIVE_FOLDER_ID:
        raise RuntimeError(
            "Nuvem nao configurada: copie config.example.py para config.py "
            "e preencha SPREADSHEET_ID e DRIVE_FOLDER_ID."
        )

    if _sheet is None:
        # Sheets continua com a Service Account (só escopo de Sheets).
        # Abrir por ID evita precisar de escopo de Drive pra achar por nome.
        sheets_creds = ServiceAccountCredentials.from_service_account_file(
            SERVICE_ACCOUNT_PATH, scopes=SHEETS_SCOPES
        )
        gc = gspread.authorize(sheets_creds)
        _sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

    if _drive_service is None:
        _drive_service = build(
            "drive", "v3", credentials=_obter_credenciais_drive(interativo)
        )


# ============================================================
# ENVIO
# ============================================================
def _upload_foto_para_drive(caminho_foto):
    """Sobe a foto pro Drive e retorna o link de visualização."""
    nome_arquivo = os.path.basename(caminho_foto)
    file_metadata = {"name": nome_arquivo, "parents": [DRIVE_FOLDER_ID]}
    media = MediaFileUpload(caminho_foto, mimetype="image/jpeg")
    arquivo = _drive_service.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    return arquivo.get("webViewLink", "")


def sincronizar_pendentes():
    """
    Envia pra nuvem todos os registros com enviado_nuvem = 0, do mais antigo
    pro mais novo. Retorna quantos foram enviados nessa rodada.
    """
    pendentes = registro.listar_pendentes()
    if not pendentes:
        return 0

    try:
        _inicializar()
    except Exception as e:
        print(f"[cloud_sync] Nuvem indisponivel ({len(pendentes)} pendente(s) ficam pra depois): {e}")
        return 0

    enviados = 0
    for r in pendentes:
        try:
            if os.path.exists(r["caminho_foto"]):
                link_foto = _upload_foto_para_drive(r["caminho_foto"])
            else:
                # Foto sumiu do disco: não trava a fila, registra sem link.
                link_foto = ""
                print(f"[cloud_sync] AVISO: foto do registro #{r['id']} nao encontrada.")

            _sheet.append_row([r["timestamp"], r["tipo"], link_foto])
            registro.marcar_enviado(r["id"])
            enviados += 1
            print(f"[cloud_sync] Registro #{r['id']} enviado ({r['tipo']})")
        except Exception as e:
            # Provavelmente caiu a internet: para aqui e tenta de novo na próxima rodada.
            print(f"[cloud_sync] ERRO no registro #{r['id']}, tento de novo depois: {e}")
            break

    return enviados


# --- Uso manual: python3 cloud_sync.py ---
if __name__ == "__main__":
    print("Conectando (pode abrir o navegador se precisar autorizar)...")
    _inicializar(interativo=True)
    n = sincronizar_pendentes()
    print(f"Concluido. {n} registro(s) enviado(s).")
