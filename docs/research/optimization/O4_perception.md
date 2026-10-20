# O4. Восприятие и жесты на Jetson Nano: оптимизация и адекватность

Срез: 20.10.2026. Только исследование: код и конфиги не менялись. Сверено с `neurobot/perception` (README, `predict_live.py`,
`gesture_logic.py`), `docs/neurobot/PLAN.md` §2.5, §3, §13, `ANALYSIS.md`, `research/R1_demo.md`, `research/R7_robot_slides.md`
(R7b), `neurobot/sim/budget/README.md` §6, `experiments/results/neurobot_20261018_acceptfix2/`.

Пометки статуса: **[есть]** / **[частично]** / **[нет]**. Основания: [код], [ист.] (§11), [замер] (наш, двойник — ориентир),
[оценка], [гипотеза]. Ни одной цифры с настоящего Nano у нас нет: все «на Nano» — источники или оценка.

## 0. Коротко

1. **Задержку K1 задаёт не детектор, а END_GAP.** На двойнике K1 p50 / p95 0,67 / 0,95 с, из них 0,5 с — ожидание
   конца жеста, ещё 0,1–0,3 с — кадры «кулака» после t0. TensorRT сэкономит десятки мс, END_GAP — сотни.
2. **Новая находка по нашим прогонам:** все преждевременные реакции («ранние», робот поехал до конца жеста) — на жестах,
   которые уходят за **верхний край кадра**: 12 из 12 в `lab_v5`, 20 из 20 в `long_v5`. Среди остальных обрезанных
   только 16 из 63 и 9 из 50 (§2.1). Это 15–27 % жестов, и END_GAP не различает «рука ушла» и «рука вышла за кадр».
3. **По памяти главный резерв — perception без torch.** Сейчас это самый большой процесс: 1305 МиБ PSS, из них CUDA-контекст
   PyTorch ≈ 1024 МиБ (балласт по источнику). TensorRT FP16 + numpy-SNN дают оценочно −0,5…−0,8 ГиБ и быстрый рестарт.
4. **INT8 на Nano не применим** (Maxwell, сс 5.3: в матрице TensorRT INT8 = «No»). Остаётся только FP16.
5. **YOLO получает BGR, а обучен на RGB** (G7, [код] yolov5@725b922e). Это самая дешёвая возможная правка точности, но
   менять её можно только после A/B на записях лаборатории.
6. **Цель K1 ≤ 0,5 с строже, чем требуют исследования HRI.** Для дискретной команды ориентир — ≈1 с (Nielsen; Shiwa и др.,
   2009). Важнее мгновенная (≤ 0,1–0,2 с) обратная связь «вижу руку / принял жест». У нас её нет.

## 1. Что есть сейчас (сверка с кодом)

| Узел | Сейчас | Где |
|---|---|---|
| Детектор | YOLOv5s (7,0 млн парам.), 1 класс `hand`; torch.hub AutoShape, `size=320` → вход сети 320×256 (letterbox `auto=False`, кратно 32); FP16 `.half()` на CUDA; conf 0,35, iou 0,45, `max_det=1`; обучен при imgsz 256 | `predict_live.py:660–667, 813`; R1 §5.1 |
| Цвет | кадр BGR (RealSense `bgr8` / тема → BGR) → AutoShape без перестановки | `predict_live.py:803–813`; yolov5 `common.py` |
| Конвейер | захват отдельно («последнее побеждает» для `--camera ros`), YOLO синхронно на каждом кадре в потоке «зрения»; пауза YOLO в движении (L6) | `predict_live.py:791–818` |
| Сегментация | конец — END_GAP 0,5 с без рамки; окно 8 с; ≥ 3 рамок; путь ≥ 0,1 ширины; мёртвая зона 0,02; `--end-on-still` под флагом | `gesture_logic.py` |
| SNN | 784-128-4 LIF, rate-кодирование 25 шагов × 5 голосований, зерно 0; «не распознано» при отрыве top-1 − top-2 < 0,04 спайка на шаг; torch на GPU | `predict_live.py:183–260`, `gesture_logic.decide` |
| Предобработка | прямоугольники 10 % (лаборатория) или `--polyline`/`--canvas-norm` под флагами | README perception |
| TensorRT, ONNX | нет; в PLAN — опция (§7 строка «Опции»: тест эквивалентности IoU ≥ 0,95 на ≥ 200 кадрах; §11 «Предложения») | PLAN §7, §11 |
| numpy-SNN | описан в R1 §5.3 (0,33 мс на M4), **кода в проекте нет** (скрипты были в scratchpad сессии R1) | R1 §5.3, §11 |
| Темп на стенде | FakeYolo 10 к/с «как Jetson» [оценка R1: 9–16 к/с на PyTorch] | gesturegen `det_fps` |

## 2. Наши данные: что они говорят

### 2.1. Ранние реакции = выход руки за верхний край кадра [замер, двойник]

Сопоставлены `gestures.jsonl` (флаг `early`, реакция раньше t0) и `truth_schedule.jsonl` (доля потерянных рамок `cut`,
`cut_top`) итоговых прогонов (скрипт — scratchpad `o4/early.py`, в проект не переносился):

| Прогон | Жестов | Ранних | Из ранних с `cut_top` > 0 | Средняя доля обрезки у ранних | Не ранние с `cut_top` > 0 |
|---|---|---|---|---|---|
| `lab_v5` | 80 | 12 | **12** | 0,38 | 16 из 63 (средняя 0,10) |
| `long_v5` | 75 | 20 | **20** | 0,45 | 9 из 50 (средняя 0,07) |

Механизм: рука уходит вверх за кадр дольше чем на END_GAP. Эпизод кончается, SNN распознаёт префикс, робот едет за
≈1,1 с до конца жеста (p50 ранних −1,04…−1,17 с, `compare.md`). У A0 (END_GAP 1,5 с) ранних всего 3. Оговорка: геометрия
стенда — гипотеза R7b RS4 (камера 0,66 м, f = 616, `fit=False`). На роботе доля другая, но механизм тот же.

### 2.2. Из чего складывается K1

На двойнике t0 → `/cmd_vel` p50 0,67 с. Это 1–3 кадра «кулака» после t0 (gesturegen `end=fist`), END_GAP 0,5 с, фаза
кадра 0–0,1 с и SNN. На Nano добавится T_loop (YOLO PyTorch 60–110 мс [оценка R1]) — до одного такта к фазе.

### 2.3. Память и CPU (стенд, `budget/README` §6)

- perception: PSS 1305 МиБ = код 281 + балласт CUDA 1024 (по источнику: PyTorch на Nano даёт +1024…1334 МиБ).
- При MemoryMax 1536 perception первым уходит в OOM-петлю, и ≈1,3 ядра уходят на повторные импорты torch.
- CPU perception без YOLO — 13 % ядра A57. Предобработка YOLO, NMS и ожидание GPU на стенде не учтены.

## 3. Детектор на Nano: TensorRT, FP16, INT8, реальные цифры

**Точность вычислений:**
- **INT8.** В TensorRT 8.2.5 для compute capability 5.3 INT8 — «No», FP16 — «Yes» [ист. 1]. На форуме NVIDIA для Nano —
  «use FP32 or FP16» [ист. 2]. Q-engineering на Nano: INT8 не даёт прироста FPS, а mAP заметно хуже [ист. 3].
  **Вывод: только FP16.**
- **FP16 в PyTorch** у нас уже есть (`.half()`) [есть].

**Цифры на Nano.** Прямого замера YOLOv5s при 320×256 FP16 TensorRT в источниках **не нашёл**. Ближайшее:

| Что | Цифра | Источник | Оговорка |
|---|---|---|---|
| YOLOv8n TensorRT FP16 | 19 к/с | Q-engineering [ист. 3] | только инференс; вход не указан (вероятно 640) |
| YOLOv8s TensorRT FP16 | 9,25 к/с | там же | то же |
| YOLOv5nu TensorRT FP16 | 20 к/с | там же | то же |
| YOLOv5s PyTorch, 640×640 | ≈120 мс | форум NVIDIA [ист. 4] | точность не указана |
| YOLOv5s .engine 640×640 / 640×480 | ≈140 / ≈70 мс | там же | 140 мс — engine не той формы: форма engine должна совпадать с фактической |
| YOLOv5n на Nano | 12,34 к/с, 0,154 мВт·ч/кадр | Machado и др., 2022 [ист. 5] | рантайм и вход в аннотации не указаны |
| YOLOv5-Nano TensorRT | ≈35 мс | Bulut и др., IPMV 2023 [ист. 6] | в аннотации «commercially available edge device», что это Nano — не подтвердил |

**Оценка для нас [оценка]:**
- YOLOv5s при 320×256 — 16,5 GFLOPs × (320·256)/(640²) ≈ 3,3 GFLOPs; YOLOv8n при 640 — 8,7 GFLOPs (Ultralytics).
- Пересчёт по FLOPs от 19 к/с YOLOv8n даёт ≈25–35 мс на инференс при TensorRT FP16. Плюс предобработка и NMS на CPU,
  итого цикл ≈35–50 мс против 60–110 мс у PyTorch.
- Это ±50 %: на малых входах Nano упирается в накладные расходы запуска ядер. Мерить `trtexec --fp16` на роботе.

**Как собрать под JetPack 4.6 + Python 3.8:**
- **Python-биндинги.** JetPack 4 даёт биндинги TensorRT только для Python 3.6; для 3.8 их собирают из исходников
  (форумы NVIDIA, случаи неудачных сборок) [ист. 9]. Образ Q-engineering Ubuntu 20.04 для Nano уже содержит TensorRT
  8.0.1.6 с колесом `cp38` и PyTorch 1.13 [ист. 8]. Стек слайда 4 (Ubuntu 20.04, Py 3.8, torch 1.13) похож на этот образ
  [гипотеза]. Проверить на роботе `python3 -c "import tensorrt"`.
- **Поддержка в yolov5.** На коммите `725b922e` `DetectMultiBackend` загружает `.engine` (`import tensorrt`, TRT ≥ 7)
  [ист. 12], но torch остаётся в процессе.
- **Форма engine.** Строить на самом Nano (`trtexec`, engine не переносим) ровно под форму AutoShape: 256×320 (h × w).
  Квадратный 320×320 — +25 % работы на поля. Форум показывает и цену несовпадения формы: 140 против 70 мс [ист. 4].
- **Память.** CUDA-контекст TensorRT меньше, если отключить источники тактик cuDNN/cuBLAS. На JetPack 4.6 у модератора
  NVIDIA MNIST в `trtexec` занимал 832,7 МБ, а с `--tacticSources=-cudnn` — 527,5 МБ; не все слои так собираются [ист. 7].
  Что именно мерили (процесс или вся система), не указано.

## 4. Вход, ROI, трекинг, пропуск кадров, конвейер

- **Размер входа.**
  - Обучение шло при 256, инференс идёт при 320 (G7, RS12). Ultralytics: «лучшие результаты — при том же `--img`, что
    при обучении» [ист. 11].
  - Engine 192×256 даёт −40 % FLOPs к 320×256. Но кисть на 2 м становится ≈ 9×17 px (R1 §6.3: 23×42 px при 640).
    Решать A/B на записях, по recall на дальних жестах [нет].
- **Цвет.** AutoShape ждёт RGB (`cv2.imread(...)[:,:,::-1]  # BGR to RGB`), обучение тоже RGB (`dataloaders.py:843`), а
  мы подаём BGR [ист. 12, код] [частично: G7 записан, отложен до записей]. Правка — одна строка `[..., ::-1]`. При
  переходе на TensorRT цвет выбирается явно, поэтому A/B нужен в любом случае.
- **ROI и трекинг.**
  - MediaPipe Hands — двухступенчатая схема «детектор ладони + модель ключевых точек» [ист. 14]. Обрезка по прошлому
    кадру в аннотации не описана — это известная практика, источник её не подтверждает.
  - Для нас ROI 2–3× рамки с меньшим engine помогает прежде всего дальним (мелким) рукам. Сложность — fallback на полный
    кадр [нет; низкий приоритет].
- **Детекция «по нужде», а не на каждом кадре.**
  - Детекцию чередуют с трекингом: планировщик «детектировать или трекать» лучше фиксированного пропуска кадров (Luo и
    др., AAAI 2019) [ист. 21]. MARLIN запускает DNN только при новом объекте или потере трека: энергия до −73,3 % [ист. 22].
  - В «Streaming Perception» задержка иногда минимальна, если «ничего не делать» (простаивать), а асинхронный трекинг с
    прогнозом улучшает потоковую точность [ист. 20].
  - Для жестов нужна рамка на каждом кадре эпизода: по R1 §6.3 меньше рамок — бледнее холст и больше «немых» SNN.
    Поэтому трекер вместо детектора внутри жеста не нужен. Полезна только **адаптивная частота в простое** (§8, M2).
- **Продление трека по слабым детекциям (идея ByteTrack).**
  - ByteTrack (ECCV 2022) не выбрасывает детекции ниже порога, а связывает их с существующими треками. Так он
    восстанавливает перекрытые объекты; IDF1 растёт на 1–10 пунктов [ист. 19].
  - У нас `conf=0,35` и `max_det=1` отсекают такие рамки ещё до сегментатора. Если рука на кадре дала 0,2, END_GAP
    начинает отсчёт [нет].
- **Асинхронный конвейер.**
  - Захват уже отвязан: `--camera ros` использует ящик «последнее побеждает» [есть].
  - Перекрытие CPU-предобработки следующего кадра с GPU текущего даст ≈5–15 % такта [оценка]. Мало по сравнению с END_GAP.
- **Кадры из темы в Foxy.**
  - Разбор 640×480×3 (≈0,9 МБ) в rclpy на 30 к/с у нас не мерили (README perception).
  - Сериализация больших сообщений в rclpy медленная: поправлено после Foxy, но не для всех типов. `take_message`
    держит GIL [ист. 36].
  - Дёшево: частоту цвета в launch `realsense2_camera` поставить равной темпу детектора (15 вместо 30) [нет].

## 5. Лёгкие детекторы рук 2024–2026

| Вариант | Цифры | Применимость к нам |
|---|---|---|
| YOLOv5n / YOLOv8n / YOLO11n (Ultralytics) | COCO 640: v5n 1,9 млн / 4,5 GFLOPs (NanoDet README); v8n 3,2 млн / 8,7 GFLOPs, mAP 37,3; YOLO11n 2,6 млн / 6,5 GFLOPs, mAP 39,5 [ист. 17, 18] | нужно **переобучение** на данных лаборатории; PLAN §8 «не переобучаем» → только предложение лаборатории. Для 1 класса v5s избыточна, но после TensorRT детектор — не узкое место |
| NanoDet-Plus-m | 320: 1,17 млн, 0,9 GFLOPs, COCO mAP 27,0; ARM Kirin 980 ≈ 12 мс (ncnn) [ист. 17] | экспорт ONNX/ncnn; TensorRT в README не заявлен; тоже переобучение |
| HaGRIDv2: YOLOv10n hand detector | 2,7 млн, mAP 87,9 (hand), CPU Xeon 75,8 мс; класс `no_gesture` разнообразили → **ложных срабатываний в 6 раз меньше**; есть классы жестов (кулак, ладонь); лицензия — вариант CC BY-SA 4.0 [ист. 16] | готовые веса без нашего обучения; полезно для K7 (ложные боксы) и как явный маркер конца жеста («кулак»). Требует согласия лаборатории, A/B и экспорта YOLOv10 под TensorRT 8.x (не проверял) |
| MediaPipe Hands | Nano: ≈22 к/с с GPU (сторонняя сборка, Python 3.6, L4T 32.5.1), 13–14 к/с CPU / ≈40 к/с GPU (своя сборка, JetPack 4.6); на Orin Nano GPU-делегат не завёлся → ≈5 к/с [ист. 15, сводка форумов] | официального GPU-пакета под aarch64 + Python 3.8 нет; ключевые точки нам не нужны (траектория по центру). **Не рекомендую** |

Вывод: детектор менять не нужно. Нужно сделать engine TensorRT из **их же** весов. Замена модели — вопрос к лаборатории
после записей; самый интересный кандидат по K7 — HaGRIDv2 (`no_gesture`, кулак).

## 6. Распознавание траектории и ранняя классификация

**Метод.** У нас SNN 784-128-4 по растру 28×28 траектории, а сеть обучена на EMNIST.
- Растр теряет порядок и направление точек: сторону G5 считаем отдельно, направление круга не различается.
- Домен обучения — символы, а не траектории рамок (R7b §4.1).
- На двойнике верных реакций 38 из 80, ошибки в основном в классе SNN (PLAN §13.5).

**Альтернативы:**

| Метод | Что даёт | Ограничения |
|---|---|---|
| **$1** (Wobbrock и др., UIST 2007) [ист. 23] | > 97 % с 1 шаблоном, 99 % с 3+ (16 жестов, стилус); близко к DTW, лучше Rubine; ~100 строк, без обучения | по умолчанию инвариантен к повороту, масштабу и положению: «не различает вверх/вниз», горизонталь и вертикаль портит неравномерное масштабирование. Авторы предлагают решение: поворот выключается, 1D-штрихи масштабируются равномерно. Для наших классов (горизонталь, вертикаль, сторона) **обязательно** |
| **Protractor** (Li, CHI 2010) | быстрее $1 [ист. 24]; режим с учётом ориентации — по памяти, текст не открывал [проверить] | — |
| **$Q** (Vatavu и др., MobileHCI 2018) [ист. 25] | точечные облака, для «low-resource devices»: до 142× быстрее $P без потери точности; BSD | облако без порядка точек; сторону и направление брать отдельно (у нас так и есть) |
| DTW 1-NN | учитывает порядок; в UIST 2007 по точности ≈ $1 | O(n²) на шаблон, на десятках шаблонов это мс [оценка] |
| LSTM / CNN по траектории | air-writing по 3D-траектории: 99,32 % на 6DMG, LSTM лучше CNN (Alam и др., Sensors 2020) [ист. 29] | нужен датасет и обучение — не наш путь (PLAN §8) |

**SNN как вычисление:**
- На GPU SNN не экономит энергию: аппаратный анализ показывает, что SNN конкурирует с эффективными ANN только при очень
  высокой разреженности спайков и на событийном железе (Dampfhoffer и др., 2022) [ист. 31].
- Прямое кодирование входа точнее rate-кодирования при малом числе шагов, rate — устойчивее (Kim и др., ICASSP 2022)
  [ист. 30]. Без переобучения менять кодирование нельзя [нет; не рекомендую].
- Вывод: SNN — требование лаборатории (демонстрация NeuroChip), а не оптимизация. Её цена (единицы–десятки мс на жест) —
  не узкое место.

**Ранняя и потоковая классификация:**
- **Molchanov и др., CVPR 2016.** Для HCI желательна «negative lag» — распознать до конца жеста; R3DCNN + CTC на
  несегментированном потоке [ист. 27].
- **Köpüklü и др., 2019 (arXiv 1901.10323)** [ист. 26]:
  - уверенность — разрыв top-1 − top-2 взвешенных вероятностей;
  - если разрыв ≥ τ_early, класс выдаётся сразу, иначе ждут, пока детектор жеста «выключится», и порог τ_late = 0,15;
  - на EgoGesture при τ_early = 0,4 срабатывание в среднем на ≈9 кадров раньше ценой −1,71 % точности.
  - **Наш `margin` в `decide()` — та же мера.** G1 сделан правильно; два порога — естественное продолжение.
- **Обзор и бенчмарк ранней классификации рядов** — Renault и др., TMLR 2025, библиотека `ml_edm` [ист. 28]: компромисс
  «точность против ранности» — основной предмет.
- **Риск для нашего словаря:** префикс круга — та же дуга, что «Вперёд» (слайд 12). Коммит **посреди** жеста опасен для
  K7. Безопасный вариант «раньше» — относительно END_GAP, а не относительно движения руки (§8, H3).

## 7. Допустимые задержки в HRI

- **0,1 / 1 / 10 с** — «мгновенно», «не рвёт ход мысли», «удерживает внимание» (Nielsen 1993; Miller 1968; Card и др.,
  1991) [ист. 32].
- **Коммуникационный робот** (Shiwa и др., IJSR 2009): предпочтение пиком при ответе через **1 с**. Длинные задержки
  смягчает «филлер» — сигнал «сейчас отвечу» [ист. 33].
- **Непрерывная телеуправляемая езда:**
  - точность падает с 200–300 мс (arXiv 2508.18074) [ист. 34];
  - к задержке начала движения пользователи не адаптируются, к медленности робота — адаптируются (Rakita и др., HRI 2020)
    [ист. 35].
  - Это непрерывное управление, а у нас дискретные команды. Переносить эти пороги прямо нельзя.
- **Gesture-specific порогов для дискретной команды мобильному роботу в источниках не нашёл.**

Вывод для нас:
- K2 (до движения) p95 ≤ ≈1 с — обоснованная цель; на двойнике 1,07 с, на границе.
- K1 ≤ 0,5 с строже нужного. Это хорошо, но не обязательно.
- Подтверждение ≤ 0,1–0,2 с от «рука видна» / «жест принят» по литературе важнее, чем ещё −0,2 с K1 [нет]. R1 §8 п. 4
  это уже рекомендовал.

## 8. Рекомендации

Эффект — на метрики ВКР (K1/K2, K7, RAM, CPU, TTR). Трудоёмкость — рабочие дни с тестами. Колонка «Nano/Foxy/3.8»:
✓ — применимо как есть, ⚠ — с условием.

| # | Что | Статус | Эффект [оценка, если не сказано] | Труд | Nano/Foxy/3.8 | Приор. |
|---|---|---|---|---|---|---|
| H1 | **END_GAP с учётом края кадра.** Последняя рамка у края (особенно верхнего, ≤ 1–2 % высоты) → ждать дольше (0,8–1,0 с) возвращения руки. Траектория с большой долей обрезки → «не распознано». Исчезла внутри кадра → обычный (или короткий, H3) END_GAP | [нет] | убирает механизм всех ранних реакций двойника (15–27 % жестов, §2.1) → K7 и верные реакции; затем позволяет короткий END_GAP внутри кадра | 1 | ✓ (чистый Python, `gesture_logic`) | **высокий** |
| H2 | **Гистерезис уверенности (идея ByteTrack).** Модель с conf 0,1; эпизод начинается с ≥ 0,35, продолжается рамкой ≥ 0,1 в окне вокруг прогноза (постоянная скорость). Стенду: FakeYolo должен отдавать слабые рамки на «промахах» | [нет] | меньше разрывов посреди жеста → END_GAP внутри кадра 0,3 с без роста разрывов → K1 −0,2 с | 1–1,5 | ✓ | **высокий** |
| H3 | **Два порога решения (Köpüklü 2019).** На первом пустом кадре считать SNN (numpy или torch). Если `margin` ≥ τ_early (высокий) и прошло ≥ 0,2–0,3 с без рамки — команда. Иначе ждать END_GAP с `min_margin`. τ — по записям, отдельно доля разрывов и K7 | [нет] | K1 p50 −0,2…−0,3 с для уверенных жестов; p95 к ≤ 0,6 с [оценка]; риск K7 → обязательна калибровка | 1 | ✓ | **высокий** |
| H4 | **YOLO → TensorRT FP16**, engine 256×320 (h × w) под форму AutoShape, `trtexec` на Nano. Первый шаг — через `DetectMultiBackend` (torch остаётся) | [частично] (опция PLAN) | цикл 60–110 → ≈35–50 мс; больше рамок на жест (R1 §6.3: при 7 → 30 к/с «немых» SNN меньше); K1 p95 −40–80 мс; CPU ниже (меньше Python-запусков ядер) | 1–2 + замер | ⚠ нужен `tensorrt` для Python 3.8 (образ Q-engineering — есть; иначе сборка) | **высокий** |
| H5 | **perception без torch**: TensorRT (+ pycuda) + numpy-SNN + numpy-предобработка (аналоги `F.interpolate`, R1 §6.3). Сверка: IoU рамок ≥ 0,95 на ≥ 200 кадрах (PLAN), спайки — по дампам `spikes_out`. Попробовать `--tacticSources=-CUDNN,-CUBLAS` | [нет] (numpy-порт только описан в R1) | RAM −0,5…−0,8 ГиБ (крупнейший процесс; ветка GNOME перестаёт быть дефицитной); рестарт perception без импорта torch → K5/K9; нет OOM-петли с CPU на импорт torch | 3–5 | ⚠ то же; numpy на 3.8 ✓ | **высокий** (по RAM) |
| H6 | **RGB вместо BGR на входе YOLO** (G7): A/B на записях лаборатории — recall и IoU | [частично] (известно, отложено) | точность детектора (величина неизвестна [гипотеза]) | 0,5 + A/B | ✓ | **высокий** (дёшево) |
| M1 | Вход 256 (как обучение): engine 192×256 | [нет] | −40 % FLOPs детектора; точность ± — A/B на дальних | 0,5 + A/B | ✓ | средний |
| M2 | **Адаптивная частота детекции**: нет руки N с → 3–5 Гц; первая рамка → полный темп. Плюс пауза в движении (уже есть) | [частично] (L6 есть) | в простое GPU-нагрузка −50–70 % → тепло и троттлинг (R9), энергия; цена — реже рамки в первые 0,2–0,3 с жеста (жесты 3–7 с) | 0,5 | ✓ | средний |
| M3 | Частота цвета `realsense2_camera` = темп детектора (15 к/с); замерить CPU разбора `Image` в rclpy Foxy | [нет] | CPU и трафик DDS −50 % на кадрах | 0,5 (конфиг + замер) | ✓ (имена параметров зависят от версии realsense-ros) | средний |
| M4 | **Обратная связь оператору**: «рука видна» ≤ 0,1 с, «принят: X» / «не понял» сразу по `/neurobot/gesture` (экран, LED или звук — что есть на роботе) | [нет] | воспринимаемая задержка, меньше повторных жестов → меньше ложных; вычислений ≈ 0 | 0,5–1 | ✓ (железо — вопрос лаборатории) | средний |
| M5 | **$Q / Protractor** по центрам рамок (с ориентацией, с равномерным масштабом для линий): (а) базовая линия ВКР против SNN; (б) опционально «действовать, только если SNN и $Q согласны» | [нет] | объясняет, где ошибка: в рамках или в SNN; (б) — K7 ниже ценой большего «не распознано»; вычисления — мс [оценка] | 1–2 | ✓ numpy | средний |
| M6 | Если остаёмся на torch: `cudnn.benchmark = True` при фиксированной форме | [нет] | неизвестно на Maxwell; возможен рост рабочей памяти — мерить | 0,1 | ✓ | низкий |
| L1 | ROI-детекция вокруг прошлой рамки маленьким engine + полный кадр при потере | [нет] | recall дальних рук, такт меньше | 2–3 | ✓ | низкий |
| L2 | Другой детектор (HaGRIDv2 YOLOv10n, YOLOv8n/11n, NanoDet) | [нет] | K7 (`no_gesture`), маркер конца «кулак» | 3+ и решение лаборатории | ⚠ экспорт под TRT 8.0/8.2 не проверял | низкий |
| L3 | Трекер вместо детектора на части кадров | [нет] | не нужен при ≥ 15–20 к/с TensorRT; траектории нужна рамка на каждом кадре | — | — | не делать |
| L4 | MediaPipe Hands | [нет] | — | — | ✗ (нет GPU-сборки под 3.8 aarch64) | не делать |
| L5 | INT8 | [нет] | — | — | ✗ (сс 5.3) | не делать |
| L6 | `collect_diag.sh`: `dpkg-query` по `libnvinfer*`, `tensorrt`, `python3-libnvinfer*`, `cuda-*`; `python3 -c "import tensorrt"` для каждого интерпретатора | [частично] (`nvidia-l4t-*` и `pip freeze` есть) | узнаём, есть ли TensorRT для 3.8 до работы над H4/H5 | 0,2 | ✓ | высокий (дёшево) |

**Порядок:**
1. L6 и записи лаборатории (5–10 на жест, уже просим в LAB_HANDOUT).
2. H6 и M1 — A/B на записях.
3. H1–H3 — на двойнике, затем на записях.
4. H4 → H5 — на роботе (HIL).
5. M2–M5 — по ходу работ.

**Как мерить:**
- H1–H3 — счётчики стенда: ранние, разрывы, K1, K7.
- H4–H5 — `tegrastats`, PSS, такт цикла и TTR на Nano.
- M5 — точность на записях.

## 9. Вердикты по текущим решениям

| Решение | Вердикт | Почему |
|---|---|---|
| FP16 (`.half()`) | адекватно | Nano поддерживает FP16, INT8 — нет [ист. 1, 2] |
| YOLOv5s в PyTorch через torch.hub | устарело для прод-пути | стандарт для Nano — TensorRT engine; PyTorch стоит ≈1 ГиБ RAM и медленнее [ист. 3, 4, 7; budget §6] |
| Инференс 320 при обучении 256 | спорно | Ultralytics советует тот же размер [ист. 11]; решать A/B |
| BGR в AutoShape | ошибочно по контракту yolov5 [код, ист. 12]; влияние на точность — [гипотеза] | исправлять только после A/B (G7) |
| END_GAP 0,5 с | адекватно как компромисс, но неполно | не различает «рука ушла» и «вышла за кадр»: все ранние реакции двойника — обрезка сверху (§2.1) |
| Окно 8 с, ≥ 3 рамок, путь ≥ 0,1, мёртвая зона | адекватно | под медленные жесты R7b; путь калиброван на синтетике (README perception) |
| «Не распознано» по отрыву top-1 − top-2 | адекватно | та же мера уверенности, что у Köpüklü 2019 [ист. 26]; порог 0,04 — калибровать |
| 5 голосований с зерном | адекватно | детерминизм; цена ×5 мала |
| SNN по растру EMNIST-стиля | спорно как метод, оправдано как требование лаборатории | теряет порядок и направление; домен не тот; энергетического смысла на GPU нет [ист. 31]; нужна базовая линия (M5) |
| Пауза YOLO в движении | адекватно | то же, что «детекция по нужде» [ист. 20, 22] |
| Темп стенда 10 к/с | адекватно как оценка, устареет после H4 | стенд гонять и при 20 к/с |
| Цель K1 p95 ≤ 0,5 с | спорно (строже литературы) | для дискретной команды ориентир ≈1 с + мгновенная обратная связь [ист. 32, 33] |

## 10. Замерить и спросить на роботе

**Замерить:**
- такт цикла по стадиям (кадр, предобработка, YOLO, NMS, SNN) в PyTorch и в `trtexec --fp16` на 256×320 и 192×256;
- `tegrastats` и PSS perception до и после;
- `nvpmodel` и `jetson_clocks`; троттлинг за 30 мин непрерывной детекции;
- CPU разбора `Image` в rclpy при 30 и 15 к/с.

**По записям** (rosbag цвета, 5–10 на жест):
- recall и IoU YOLO: BGR против RGB, 320 против 256;
- статистика пропусков рамок внутри жеста: длины провалов → END_GAP и H2;
- доля жестов, выходящих за край кадра (RS4): высота камеры, наклон, дистанция оператора.

**Спросить лабораторию:**
- есть ли на роботе экран, LED или динамик для обратной связи (M4);
- допустима ли замена детектора на веса HaGRIDv2 (L2).

## 11. Источники

1. NVIDIA TensorRT 8.2.5 Support Matrix (сс 5.3: FP16 — Yes, INT8 — No). https://docs.nvidia.com/deeplearning/tensorrt/archives/tensorrt-825/support-matrix/index.html
2. NVIDIA Developer Forums, «Can Jetson Nano import INT8» (ответ модератора: Nano — FP32/FP16). https://forums.developer.nvidia.com/t/can-jetson-nano-import-int8/178500
3. Q-engineering, YoloV8-TensorRT-Jetson_Nano (FP16; Nano: YOLOv8n 19 к/с, YOLOv8s 9,25, YOLOv5nu 20; INT8 без прироста; только инференс). https://github.com/Qengineering/YoloV8-TensorRT-Jetson_Nano
4. NVIDIA Developer Forums, «Yolov5 + TensorRT results seems weird on Jetson Nano 4GB» (≈120 мс PyTorch 640; engine 140 → 70 мс при верной форме 640×480). https://forums.developer.nvidia.com/t/yolov5-tensorrt-results-seems-weird-on-jetson-nano-4gb/198282
5. P. Machado, I. Matic, F. de Lemos, I. K. Ihianle, D. A. Adama. Estimating the Power Consumption of Heterogeneous Devices when performing AI Inference. arXiv:2207.06150, 2022. https://arxiv.org/abs/2207.06150
6. A. Bulut, F. Ozdemir, Y. S. Bostanci, M. Soytürk. Performance Evaluation of Recent Object Detection Models for Traffic Safety Applications on Edge. IPMV 2023. https://doi.org/10.1145/3582177.3582178 (по аннотации; устройство не подтвердил)
7. NVIDIA Developer Forums: «Optimizing memory consumption on Jetson» (MNIST 832,7 → 527,5 МБ с `--tacticSources=-cudnn`, JetPack 4.6) https://forums.developer.nvidia.com/t/optimizing-memory-consumption-on-jetson/188334 ; «cuBLAS, cuDNN, and TensorRT memory release on Jetson nano» https://forums.developer.nvidia.com/t/cublas-cudnn-and-tensorrt-memory-release-on-jetson-nano/193023
8. Q-engineering, Jetson-Nano-Ubuntu-20-image (TensorRT 8.0.1.6, колесо cp38, PyTorch 1.13, JetPack 4.6.1). https://github.com/Qengineering/Jetson-Nano-Ubuntu-20-image
9. NVIDIA Developer Forums, TensorRT для Python 3.8 на JetPack 4 (только 3.6 из коробки, сборка биндингов). https://forums.developer.nvidia.com/t/tensorrt-package-for-python3-8-in-xavier-nx-with-jetpack4-6/193868 ; https://forums.developer.nvidia.com/t/tensorrt-on-jetson-nano-jetpack4-6-1-for-python3-8-cuda10-2/310939
10. Ultralytics, NVIDIA Jetson guide (образ `latest-jetson-jetpack4`; замеров для Nano нет). https://docs.ultralytics.com/guides/nvidia-jetson/
11. Ultralytics, YOLOv5 «Tips for Best Training Results» (тот же `--img` при инференсе). https://docs.ultralytics.com/yolov5/tutorials/tips_for_best_training_results/
12. ultralytics/yolov5 @ `725b922e`: `models/common.py` (AutoShape: RGB, letterbox `auto=False`, форма кратна 32; DetectMultiBackend `.engine`), `utils/dataloaders.py:843` (BGR → RGB при обучении). https://raw.githubusercontent.com/ultralytics/yolov5/725b922eb16cddd396471b5a0148297b29143962/models/common.py
13. S. Migacz. PyTorch Performance Tuning Guide (`cudnn.benchmark`). https://docs.pytorch.org/tutorials/recipes/recipes/tuning_guide.html
14. F. Zhang, V. Bazarevsky, A. Vakunov и др. MediaPipe Hands: On-device Real-time Hand Tracking. arXiv:2006.10214, 2020. https://arxiv.org/abs/2006.10214
15. MediaPipe на Jetson Nano — сообщения форумов (по сводке поиска, первоисточники не открывал): https://forums.developer.nvidia.com/t/google-mediapipe-real-time-hand-tracking-on-nano/80281 ; https://forums.developer.nvidia.com/t/hand-tracking-on-jetson-orin-nano/297754 ; https://github.com/PINTO0309/mediapipe-bin
16. A. Kapitanov и др. HaGRIDv2: 1M Images for Static and Dynamic Hand Gesture Recognition. arXiv:2412.01508, 2024. https://arxiv.org/abs/2412.01508 ; репозиторий и лицензия: https://github.com/hukenovs/hagrid
17. RangiLyu, NanoDet (таблица: NanoDet-Plus-m, YOLOv5-n, YOLOX-Nano). https://github.com/RangiLyu/nanodet
18. Ultralytics docs: YOLOv8 https://docs.ultralytics.com/models/yolov8/ ; YOLO11 https://docs.ultralytics.com/models/yolo11/
19. Y. Zhang и др. ByteTrack: Multi-Object Tracking by Associating Every Detection Box. ECCV 2022. arXiv:2110.06864. https://github.com/FoundationVision/ByteTrack
20. M. Li, Y.-X. Wang, D. Ramanan. Towards Streaming Perception. ECCV 2020. arXiv:2005.10420. https://arxiv.org/abs/2005.10420
21. H. Luo, W. Xie, X. Wang, W. Zeng. Detect or Track: Towards Cost-Effective Video Object Detection/Tracking. AAAI 2019. arXiv:1811.05340. https://arxiv.org/abs/1811.05340
22. K. Apicharttrisorn, X. Ran, J. Chen, S. Krishnamurthy, A. Roy-Chowdhury. Frugal Following: Power Thrifty Object Detection and Tracking for Mobile Augmented Reality (MARLIN). SenSys 2019. https://par.nsf.gov/servlets/purl/10201676
23. J. O. Wobbrock, A. D. Wilson, Y. Li. Gestures without libraries, toolkits or training: a $1 recognizer for user interface prototypes. UIST 2007. https://faculty.washington.edu/wobbrock/pubs/uist-07.01.pdf ; страница $-семейства (Protractor, Li 2010): https://depts.washington.edu/acelab/proj/dollar/index.html
24. Y. Li. Protractor: a fast and accurate gesture recognizer. CHI 2010 (по ссылке со страницы $-семейства: «улучшает скорость $1»; текст статьи не открывал). https://depts.washington.edu/acelab/proj/dollar/index.html
25. R.-D. Vatavu, L. Anthony, J. O. Wobbrock. $Q: A super-quick, articulation-invariant stroke-gesture recognizer for low-resource devices. MobileHCI 2018, Article 23. https://depts.washington.edu/acelab/proj/dollar/qdollar.html
26. O. Köpüklü, A. Gunduz, N. Kose, G. Rigoll. Real-time Hand Gesture Detection and Classification Using Convolutional Neural Networks. arXiv:1901.10323, 2019. https://arxiv.org/abs/1901.10323
27. P. Molchanov, X. Yang, S. Gupta, K. Kim, S. Tyree, J. Kautz. Online Detection and Classification of Dynamic Hand Gestures With Recurrent 3D Convolutional Neural Network. CVPR 2016. https://openaccess.thecvf.com/content_cvpr_2016/html/Molchanov_Online_Detection_and_CVPR_2016_paper.html
28. A. Renault, A. Bondu, A. Cornuéjols, V. Lemaire. Early Classification of Time Series: A Survey and Benchmark. TMLR 2025. arXiv:2406.18332. https://arxiv.org/abs/2406.18332
29. M. S. Alam и др. Trajectory-Based Air-Writing Recognition Using Deep Neural Network and Depth Sensor. Sensors 2020, 20(2):376. https://doi.org/10.3390/s20020376
30. Y. Kim, H. Park, A. Moitra, A. Bhattacharjee, Y. Venkatesha, P. Panda. Rate Coding or Direct Coding: Which One is Better for Accurate, Robust, and Energy-efficient Spiking Neural Networks? ICASSP 2022. arXiv:2202.03133. https://arxiv.org/abs/2202.03133
31. M. Dampfhoffer и др. Are SNNs really more energy-efficient than ANNs? An in-depth hardware-aware study. 2022 (HAL; журнал не проверял). https://hal-cea.archives-ouvertes.fr/cea-03852141v1
32. J. Nielsen. Response Times: The 3 Important Limits (из «Usability Engineering», 1993; ссылки на Miller 1968, Card и др. 1991). https://www.nngroup.com/articles/response-times-3-important-limits/
33. T. Shiwa, T. Kanda, M. Imai, H. Ishiguro, N. Hagita. How Quickly Should a Communication Robot Respond? Delaying Strategies and Habituation Effects. Int J Soc Robot 1, 141–155, 2009. https://doi.org/10.1007/s12369-009-0012-8
34. The Effects of Communication Delay on Human Performance and Neurocognitive Responses in Mobile Robot Teleoperation. arXiv:2508.18074, 2025 (порог 200–300 мс для непрерывного управления; авторов не выписывал). https://arxiv.org/abs/2508.18074
35. D. Rakita, B. Mutlu, M. Gleicher. Effects of Onset Latency and Robot Speed Delays on Mimicry-Control Teleoperation. HRI 2020. https://peopleandrobots.wisc.edu/publications/effects-of-onset-latency-and-robot-speed-delays-on-mimicry-control-teleoperation/
36. rclpy issue #1025 «Release Python GIL in Subscription::take_message» https://github.com/ros2/rclpy/issues/1025 ; обсуждение медленной сериализации больших сообщений в rclpy (Foxy) https://robotics.stackexchange.com/questions/98772 (не открылось, по сводке поиска)

Внутренние: `neurobot/sim/budget/README.md` §6.1–6.3; `experiments/results/neurobot_20261018_acceptfix2/compare.md`,
`runs/{lab_v5,long_v5}/{gestures,truth_schedule}.jsonl`; `docs/neurobot/research/R1_demo.md` §3, §5, §6.3;
`R7_robot_slides.md` §3.4, §4.
