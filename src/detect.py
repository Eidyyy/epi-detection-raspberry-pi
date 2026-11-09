"""
detect.py
Sistema de Deteccao de EPI (capacete + colete) com alerta sonoro
Projeto: Arquitetura de Computadores e Tecnologias Embarcadas

Deteccao + buzzer + registro local (SQLite + snapshot)
+ sincronizacao assincrona com a nuvem (Sheets/Drive) em thread de fundo

Fluxo de uma violacao:
    CONFIRMADA -> registro.registrar_violacao() (rapido, local)
               -> thread de fundo chama cloud_sync.sincronizar_pendentes()
                  (se estiver sem internet, fica pendente e envia depois)

IMPORTANTE: esta versao NAO usa mediapipe. O pacote mediapipe trava
com "Illegal instruction" no Raspberry Pi 3B porque o binario foi
compilado assumindo suporte a instrucoes de criptografia AES em
hardware, que o chip BCM2837 do Pi 3B nao possui. Em vez disso,
usamos o interpretador TFLite puro (tflite-runtime ou ai-edge-litert),
lendo diretamente os tensores de saida do modelo EfficientDet-Lite0
(treinado com tflite-model-maker), que ja vem com o pos-processamento/NMS
embutido no proprio .tflite.

Hardware:
- Raspberry Pi 3B
- Webcam Logitech BRIO (via OpenCV, indice 0)
- Buzzer ativo no GPIO17 (BCM)

Uso (a partir da raiz do repositorio):
    python3 src/detect.py

Pressione 'q' na janela de video pra sair.
"""

import os
import time
import sys
import threading

import cv2
import numpy as np

import registro
import cloud_sync

# Tenta os pacotes de interpretador TFLite em ordem de preferencia
try:
    from tflite_runtime.interpreter import Interpreter
except ImportError:
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tensorflow.lite import Interpreter
        except ImportError:
            print("[ERRO] Nenhum interpretador TFLite encontrado.")
            print("Instale um deles: pip install tflite-runtime --break-system-packages")
            print("               ou: pip install ai-edge-litert --break-system-packages")
            sys.exit(1)

try:
    from gpiozero import Buzzer
    GPIO_DISPONIVEL = True
except ImportError:
    GPIO_DISPONIVEL = False
    print("[AVISO] gpiozero nao encontrado — rodando em modo simulacao (sem buzzer real)")


# ============================================================
# CONFIGURACOES
# ============================================================

# Modelo em models/best.tflite (relativo a raiz do repositorio)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "..", "models", "best.tflite")
CAMERA_INDEX = 0
CONFIDENCE_THRESHOLD = 0.5

BUZZER_PIN = 17

TEMPO_CONFIRMACAO = 2.0   # segundos de violacao continua pra confirmar
TEMPO_COOLDOWN = 5.0      # segundos de espera antes de rearmar

INTERVALO_SYNC = 15       # segundos entre tentativas de sincronizar com a nuvem

BIP_ON = 0.4
BIP_OFF = 0.4

# ATENCAO: essa e a ordem alfabetica das 4 classes, que costuma ser a
# ordem padrao do tflite-model-maker. CONFIRME no notebook de treino
# se a ordem abaixo bate com a real do seu modelo.
# Se a deteccao mostrar a classe errada pro que a camera esta vendo,
# o problema mais provavel e essa lista estar fora de ordem.
LABELS = ["colete_capacete", "nenhum_epi", "so_capacete", "so_colete"]

CLASSE_PARA_FALTANTES = {
    "colete_capacete": set(),
    "so_colete": {"capacete"},
    "so_capacete": {"colete"},
    "nenhum_epi": {"capacete", "colete"},
}

# Mapeia o conjunto de itens faltando pro rotulo de violacao usado
# no registro (planilha/SQLite). Usa frozenset porque set() nao pode
# ser chave de dicionario.
FALTANTES_PARA_TIPO = {
    frozenset({"capacete"}): "sem_capacete",
    frozenset({"colete"}): "sem_colete",
    frozenset({"capacete", "colete"}): "ambos",
}


# ============================================================
# CONTROLE DO BUZZER
# ============================================================

class ControleBuzzer:
    def __init__(self, pin):
        self.estado_atual = "desligado"
        # active_high=False porque o modulo de buzzer deste projeto responde
        # invertido: GPIO em nivel BAIXO aciona o som, nivel ALTO desliga
        # (confirmado empiricamente: o buzzer tocava no estado NORMAL,
        # quando deveria estar em silencio, e vice-versa)
        self._buzzer = Buzzer(pin, active_high=False) if GPIO_DISPONIVEL else None

    def desligar(self):
        if self.estado_atual == "desligado":
            return
        if self._buzzer:
            self._buzzer.off()
        else:
            print("[BUZZER] desligado")
        self.estado_atual = "desligado"

    def intermitente(self):
        if self.estado_atual == "intermitente":
            return
        if self._buzzer:
            self._buzzer.blink(on_time=BIP_ON, off_time=BIP_OFF)
        else:
            print("[BUZZER] intermitente (1 EPI faltando)")
        self.estado_atual = "intermitente"

    def continuo(self):
        if self.estado_atual == "continuo":
            return
        if self._buzzer:
            self._buzzer.on()
        else:
            print("[BUZZER] continuo (2 EPIs faltando)")
        self.estado_atual = "continuo"


# ============================================================
# MAQUINA DE ESTADOS
# ============================================================

class MaquinaDeEstados:
    def __init__(self, buzzer: ControleBuzzer):
        self.estado = "NORMAL"
        self.buzzer = buzzer
        self.timestamp_mudanca = time.time()
        self.faltantes_atual = set()
        # Usados so pra "debounce" de mudanca de tipo enquanto ja
        # esta em CONFIRMADA (evita registrar violacao nova toda vez
        # que a classificacao pisca entre duas classes por 1-2 frames)
        self.faltantes_pendente = None
        self.timestamp_pendente = None

    def atualizar(self, faltantes: set, frame=None):
        """
        frame: frame BGR atual da camera (opcional). So e usado pra
        salvar o snapshot quando uma violacao e CONFIRMADA. Se None,
        o registro da violacao e simplesmente pulado.
        """
        agora = time.time()

        if self.estado == "NORMAL":
            if faltantes:
                self.estado = "SUSPEITA"
                self.timestamp_mudanca = agora
                self.faltantes_atual = faltantes
                print(f"[STATE] NORMAL -> SUSPEITA (faltando: {faltantes})")

        elif self.estado == "SUSPEITA":
            if not faltantes:
                self.estado = "NORMAL"
                print("[STATE] SUSPEITA -> NORMAL (falso alarme)")
            elif faltantes != self.faltantes_atual:
                self.faltantes_atual = faltantes
                self.timestamp_mudanca = agora
            elif agora - self.timestamp_mudanca >= TEMPO_CONFIRMACAO:
                self.estado = "CONFIRMADA"
                self.timestamp_mudanca = agora
                self.faltantes_pendente = None
                print(f"[STATE] SUSPEITA -> CONFIRMADA (faltando: {faltantes})")
                self._acionar_buzzer(faltantes)
                self._registrar_violacao(faltantes, frame)

        elif self.estado == "CONFIRMADA":
            if not faltantes:
                self.buzzer.desligar()
                self.estado = "COOLDOWN"
                self.timestamp_mudanca = agora
                self.faltantes_pendente = None
                print("[STATE] CONFIRMADA -> COOLDOWN (EPI corrigido)")
            elif faltantes == self.faltantes_atual:
                # Voltou a bater com o tipo ja confirmado -- cancela
                # qualquer mudanca pendente (foi so um flicker passageiro)
                self.faltantes_pendente = None
            elif faltantes != self.faltantes_pendente:
                # Tipo mudou e e diferente do que ja estava "em observacao":
                # comeca a contar o tempo de confirmacao de novo
                self.faltantes_pendente = faltantes
                self.timestamp_pendente = agora
            elif agora - self.timestamp_pendente >= TEMPO_CONFIRMACAO:
                # A mudanca se manteve estavel por tempo suficiente:
                # confirma de vez e so ai registra/aciona o buzzer
                self.faltantes_atual = faltantes
                self.faltantes_pendente = None
                self._acionar_buzzer(faltantes)
                self._registrar_violacao(faltantes, frame)

        elif self.estado == "COOLDOWN":
            if agora - self.timestamp_mudanca >= TEMPO_COOLDOWN:
                self.estado = "NORMAL"
                print("[STATE] COOLDOWN -> NORMAL")

    def _acionar_buzzer(self, faltantes: set):
        if len(faltantes) == 1:
            self.buzzer.intermitente()
        elif len(faltantes) == 2:
            self.buzzer.continuo()

    def _registrar_violacao(self, faltantes: set, frame):
        """
        Salva o snapshot + registro no SQLite local (rapido).
        O envio pra nuvem NAO acontece aqui: quem faz isso e a thread de
        fundo (_loop_sync), pra o video nao travar durante o upload.
        """
        if frame is None:
            print("[AVISO] Violacao confirmada mas sem frame disponivel — registro pulado.")
            return

        tipo_violacao = FALTANTES_PARA_TIPO.get(frozenset(faltantes), "desconhecido")
        try:
            id_registro = registro.registrar_violacao(tipo_violacao, frame)
            print(f"[REGISTRO] Violacao #{id_registro} salva localmente ({tipo_violacao}) — aguardando sync")
        except Exception as e:
            print(f"[ERRO] Falha ao registrar violacao ({tipo_violacao}): {e}")


# ============================================================
# SINCRONIZACAO COM A NUVEM (thread de fundo)
# ============================================================

def _loop_sync(parar: threading.Event):
    """Tenta enviar os pendentes do SQLite pra nuvem a cada INTERVALO_SYNC segundos."""
    while not parar.is_set():
        try:
            cloud_sync.sincronizar_pendentes()
        except Exception as e:
            print(f"[SYNC] Erro inesperado: {e}")
        parar.wait(INTERVALO_SYNC)


# ============================================================
# INFERENCIA TFLITE (sem mediapipe)
# ============================================================

class DetectorEPI:
    def __init__(self, model_path):
        self.interpreter = Interpreter(model_path=model_path)
        self.interpreter.allocate_tensors()

        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()

        input_shape = self.input_details[0]["shape"]
        self.input_height = input_shape[1]
        self.input_width = input_shape[2]
        self.input_dtype = self.input_details[0]["dtype"]

        # IMPORTANTE: a ordem retornada por get_output_details() NAO é
        # necessariamente [boxes, classes, scores, count] -- ela pode vir
        # embaralhada (confirmado na inspecao do modelo: indices 599, 597,
        # 600, 598 nessa ordem). O que É confiavel é: (1) o formato de cada
        # tensor, e (2) que o op TFLite_Detection_PostProcess numera suas
        # saidas de forma sequencial: boxes=indice_base, classes=+1,
        # scores=+2, count=+3.
        boxes_detail = None
        count_detail = None
        outros = []
        for d in self.output_details:
            shape = d["shape"]
            if len(shape) == 3 and shape[-1] == 4:
                boxes_detail = d
            elif len(shape) == 1:
                count_detail = d
            else:
                outros.append(d)

        if boxes_detail is None or count_detail is None or len(outros) != 2:
            raise RuntimeError(
                "Formato de saida do modelo nao reconhecido. "
                f"Detalhes: {self.output_details}"
            )

        base_index = boxes_detail["index"]
        outros.sort(key=lambda d: d["index"])
        # Pelo padrao sequencial: base=boxes, base+1=classes, base+2=scores
        classes_detail, scores_detail = outros[0], outros[1]

        self.idx_boxes = boxes_detail["index"]
        self.idx_classes = classes_detail["index"]
        self.idx_scores = scores_detail["index"]
        self.idx_count = count_detail["index"]

        print(
            f"[DetectorEPI] Tensores identificados -> "
            f"boxes={self.idx_boxes} classes={self.idx_classes} "
            f"scores={self.idx_scores} count={self.idx_count}"
        )
        self._validado = False

    def detectar(self, frame_bgr):
        img = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (self.input_width, self.input_height))
        img = np.expand_dims(img, axis=0)

        if self.input_dtype == np.float32:
            img = (img.astype(np.float32) / 255.0)
        else:
            img = img.astype(self.input_dtype)

        self.interpreter.set_tensor(self.input_details[0]["index"], img)
        self.interpreter.invoke()

        boxes = self.interpreter.get_tensor(self.idx_boxes)[0]      # [N,4] ymin,xmin,ymax,xmax (0-1)
        classes = self.interpreter.get_tensor(self.idx_classes)[0]  # [N]
        scores = self.interpreter.get_tensor(self.idx_scores)[0]    # [N]
        count = int(self.interpreter.get_tensor(self.idx_count)[0])

        if not self._validado:
            # Confere se "scores" esta mesmo no intervalo 0-1. Se nao
            # estiver, classes e scores foram identificados trocados --
            # corrige automaticamente e nao precisa checar de novo.
            valores_scores = scores[:max(count, 1)]
            fora_do_intervalo = np.any((valores_scores < 0) | (valores_scores > 1))
            if fora_do_intervalo:
                print("[DetectorEPI] AVISO: scores fora de [0,1] -- "
                      "invertendo classes/scores automaticamente.")
                self.idx_classes, self.idx_scores = self.idx_scores, self.idx_classes
                classes, scores = scores, classes
            self._validado = True

        deteccoes = []
        for i in range(count):
            if scores[i] < CONFIDENCE_THRESHOLD:
                continue
            classe_id = int(classes[i])
            if classe_id < 0 or classe_id >= len(LABELS):
                continue
            deteccoes.append({
                "label": LABELS[classe_id],
                "classe_id": classe_id,
                "score": float(scores[i]),
                "box": boxes[i],  # ymin, xmin, ymax, xmax normalizados
            })
        return deteccoes


# ============================================================
# LOOP PRINCIPAL
# ============================================================

def main():
    registro.init_db()

    buzzer = ControleBuzzer(BUZZER_PIN)
    maquina = MaquinaDeEstados(buzzer)
    detector = DetectorEPI(MODEL_PATH)

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print("[ERRO] Nao consegui abrir a webcam. Verifique a conexao/indice.")
        sys.exit(1)

    # Thread de fundo que envia os registros pendentes pra nuvem.
    # daemon=True: se o programa fechar, a thread fecha junto.
    parar_sync = threading.Event()
    thread_sync = threading.Thread(target=_loop_sync, args=(parar_sync,), daemon=True)
    thread_sync.start()

    print("Iniciando deteccao. Pressione 'q' na janela de video pra sair.")

    ultima_classe_impressa = None
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("[ERRO] Falha ao ler frame da webcam.")
                break

            h, w = frame.shape[:2]
            deteccoes = detector.detectar(frame)

            faltantes = set()
            if deteccoes:
                # Pega a deteccao de maior confianca (1 pessoa por frame)
                melhor = max(deteccoes, key=lambda d: d["score"])
                classe_detectada = melhor["label"]
                faltantes = CLASSE_PARA_FALTANTES.get(classe_detectada, set())

                if classe_detectada != ultima_classe_impressa:
                    print(f"[DEBUG] classe_id={melhor['classe_id']} "
                          f"label={classe_detectada} score={melhor['score']:.2f}")
                    ultima_classe_impressa = classe_detectada

                ymin, xmin, ymax, xmax = melhor["box"]
                x1, y1 = int(xmin * w), int(ymin * h)
                x2, y2 = int(xmax * w), int(ymax * h)

                cor = (0, 255, 0) if not faltantes else (0, 0, 255)
                cv2.rectangle(frame, (x1, y1), (x2, y2), cor, 2)
                cv2.putText(
                    frame, f"{classe_detectada} ({melhor['score']:.2f})",
                    (x1, max(y1 - 10, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, cor, 2,
                )

            # Passa o frame pra maquina de estados poder salvar o
            # snapshot no exato momento em que a violacao e confirmada.
            maquina.atualizar(faltantes, frame)

            cv2.putText(
                frame, f"Estado: {maquina.estado}",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2,
            )
            cv2.imshow("Deteccao de EPI", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        pass
    finally:
        parar_sync.set()
        buzzer.desligar()
        cap.release()
        cv2.destroyAllWindows()
        print("Encerrado.")


if __name__ == "__main__":
    main()
