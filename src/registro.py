"""
registro.py - Registro local de violações de EPI (SQLite + snapshot).

Responsabilidades:
  - Salvar o snapshot (foto) do momento da violação
  - Gravar o registro no banco SQLite local
  - Controlar o que já foi enviado pra nuvem (coluna enviado_nuvem)

Não depende de câmera, modelo ou buzzer: dá pra testar sozinho
rodando `python3 registro.py`.
"""

import os
import sqlite3
from datetime import datetime

import cv2

# --- Caminhos (relativos à pasta do script, funciona de qualquer diretório) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "violacoes.db")
FOTOS_DIR = os.path.join(BASE_DIR, "fotos_violacoes")

# --- Tipos de violação válidos ---
TIPOS_VALIDOS = ("sem_capacete", "sem_colete", "ambos")

# Mapeamento das classes do modelo -> tipo de violação.
# Classe "colete_capacete" não é violação, então não aparece aqui.
CLASSE_PARA_TIPO = {
    "nenhum_EPI": "ambos",
    "so_colete": "sem_capacete",
    "so_capacete": "sem_colete",
}


def tipo_da_classe(nome_classe):
    """Converte a classe detectada pelo modelo no tipo de violação.
    Retorna None se a classe não for uma violação (ex.: colete_capacete)."""
    return CLASSE_PARA_TIPO.get(nome_classe)


def _conectar():
    # Uma conexão por chamada: seguro caso o envio pra nuvem passe a rodar em outra thread.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Cria o banco, a tabela e a pasta de fotos (se ainda não existirem)."""
    os.makedirs(FOTOS_DIR, exist_ok=True)
    with _conectar() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS violacoes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT    NOT NULL,
                tipo          TEXT    NOT NULL
                              CHECK (tipo IN ('sem_capacete', 'sem_colete', 'ambos')),
                caminho_foto  TEXT    NOT NULL,
                enviado_nuvem INTEGER NOT NULL DEFAULT 0
                              CHECK (enviado_nuvem IN (0, 1))
            )
            """
        )
        # Acelera a busca por pendentes de sincronização
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pendentes ON violacoes (enviado_nuvem)"
        )


def registrar_violacao(tipo, frame):
    """
    Salva o snapshot e grava o registro no banco.

    tipo:  'sem_capacete' | 'sem_colete' | 'ambos'
    frame: imagem BGR do OpenCV (o frame do momento da violação)

    Retorna o ID do registro criado.
    """
    if tipo not in TIPOS_VALIDOS:
        raise ValueError(f"Tipo inválido: {tipo!r}. Use um de {TIPOS_VALIDOS}")

    init_db()  # barato e idempotente; garante que tudo existe

    agora = datetime.now()
    timestamp = agora.strftime("%Y-%m-%d %H:%M:%S")
    nome_arquivo = f"violacao_{agora.strftime('%Y%m%d_%H%M%S')}_{tipo}.jpg"
    caminho_foto = os.path.join(FOTOS_DIR, nome_arquivo)

    ok = cv2.imwrite(caminho_foto, frame)
    if not ok:
        raise RuntimeError(f"Falha ao salvar o snapshot em {caminho_foto}")

    with _conectar() as conn:
        cur = conn.execute(
            "INSERT INTO violacoes (timestamp, tipo, caminho_foto) VALUES (?, ?, ?)",
            (timestamp, tipo, caminho_foto),
        )
        return cur.lastrowid


def listar_pendentes():
    """Registros ainda não enviados pra nuvem (mais antigos primeiro)."""
    init_db()
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT * FROM violacoes WHERE enviado_nuvem = 0 ORDER BY id"
        ).fetchall()
    return [dict(l) for l in linhas]


def marcar_enviado(id_registro):
    """Marca um registro como sincronizado com a nuvem."""
    with _conectar() as conn:
        conn.execute(
            "UPDATE violacoes SET enviado_nuvem = 1 WHERE id = ?", (id_registro,)
        )


def listar_todos(limite=20):
    """Últimos registros, útil pra conferir/depurar."""
    init_db()
    with _conectar() as conn:
        linhas = conn.execute(
            "SELECT * FROM violacoes ORDER BY id DESC LIMIT ?", (limite,)
        ).fetchall()
    return [dict(l) for l in linhas]


# --- Teste isolado: python3 registro.py ---
if __name__ == "__main__":
    import numpy as np

    print("Testando registro.py...")
    init_db()

    # Frame falso (preto com um retângulo) só pra gerar um JPG
    frame_teste = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(frame_teste, (200, 100), (440, 400), (0, 255, 0), 2)

    id1 = registrar_violacao("sem_capacete", frame_teste)
    id2 = registrar_violacao("ambos", frame_teste)
    print(f"Registros criados: {id1}, {id2}")

    print("Pendentes:", [r["id"] for r in listar_pendentes()])

    marcar_enviado(id1)
    print("Após marcar o 1 como enviado, pendentes:",
          [r["id"] for r in listar_pendentes()])

    print("\nÚltimos registros:")
    for r in listar_todos():
        print(r)
