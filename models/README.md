# Modelo / Model

**`best.tflite`** — EfficientDet-Lite0 treinado com `tflite-model-maker` (Google Colab) e exportado para TensorFlow Lite.
EfficientDet-Lite0 trained with `tflite-model-maker` (Google Colab) and exported to TensorFlow Lite.

## Classes

Ordem usada pelo `src/detect.py` (lista `LABELS`) / order used by `src/detect.py`:

| ID | Classe | Significado |
|---|---|---|
| 0 | `colete_capacete` | Colete e capacete |
| 1 | `nenhum_EPI` | Nenhum EPI |
| 2 | `so_capacete` | Só capacete (falta o colete) |
| 3 | `so_colete` | Só colete (falta o capacete) |

## Saídas / Outputs

O pós-processamento (NMS) já vem embutido no modelo. As saídas são: caixas (`boxes`, formato `ymin, xmin, ymax, xmax` normalizado de 0 a 1), classes, scores e quantidade de detecções.
Post-processing (NMS) is built into the model. Outputs: `boxes` (normalized `ymin, xmin, ymax, xmax`), `classes`, `scores` and detection `count`.

> O `detect.py` identifica os tensores de saída pelo **formato (shape)** e não pela posição, porque a ordem retornada pelo interpretador pode vir embaralhada.

## Métricas / Metrics

Reportadas pelo `tflite-model-maker` ao fim do treino / reported by `tflite-model-maker` after training:

- AP50 = 100%
- AP75 = 94,7%
- AP = 78,7%

> Atenção / Note: medidas sobre o próprio conjunto de dados do projeto, sem um conjunto de teste separado confirmado. Indicam o ajuste aos dados, não garantem o mesmo desempenho em imagens novas. / Measured on the project's own dataset, with no confirmed separate test set: they reflect fit to the data and do not guarantee the same performance on new images.

## Uso / Usage

O `src/detect.py` carrega `models/best.tflite` automaticamente (`MODEL_PATH`). Para usar outro modelo, substitua este arquivo mantendo a ordem das classes.
`src/detect.py` loads `models/best.tflite` automatically (`MODEL_PATH`). To use another model, replace this file keeping the class order.
