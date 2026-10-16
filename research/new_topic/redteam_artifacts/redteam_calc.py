# Числа берутся из THESIS_CONCEPT.md (§10, §12.1, §4.2, §12.4) и из документации OpenAI (vision guide)
win_h = 16                      # окно 07–23 (§12.1)
hb = win_h*60/30                # heartbeat 30 мин
act = 45                        # CASAS "активность" ≈45/сут (§12.1)
door = 55                       # CASAS "дверь" ≈55/сут
b0_bg = hb + act
print(f"B0 фоновых триггеров/сут: heartbeat {hb:.0f} + события {act} = {b0_bg:.0f}")
print(f"Гейт без локальных правил, уровень 'дверь': отношение {b0_bg/door:.2f}x (<3x)")
s_max = b0_bg/3
print(f"H2 (>=3x) выполняется при любом числе засеянных значимых ситуаций s <= {s_max:.1f}/сут")
# H3a
print(f"H3a: каталог explicit/generic при k=20: 9782/2861 = {9782/2861:.2f}x при пороге 2x")
# H5(d)
for idle in (58,79):
    print(f"H5(г): 0,2 Вт·ч / {idle} Вт·ч простоя = {0.2/idle*100:.2f} % при пороге 5 % (запас {5/(0.2/idle*100):.0f}x)")
# H7: изображения gpt-4o-mini (OpenAI images-vision guide: base 2833, tile 5667)
base, tile = 2833, 5667
import math
w,h = 640,480
tiles = math.ceil(w/512)*math.ceil(h/512)
hi = base + tiles*tile
for summ in (30,60):
    print(f"H7: кадр 640x480 gpt-4o-mini low={base} ток., high={hi} ток.; сводка {summ} ток. -> {base/summ:.0f}x..{hi/summ:.0f}x при пороге 10x")
# Цена: flex -50% (RouterAI service tiers) vs цель 3x
print("Flex-режим RouterAI: 'обычно −50%' цены при той же модели -> 2x по ₽ без изменения агента; цель 3x по ₽ наполовину достигается флагом")
# масштаб денег: GAP1 модель 6.46 ₽/сут B0
for r in (3,):
    save = 6.46*(1-1/r)
    print(f"Экономия при 3x от 6,46 ₽/сут: {save:.2f} ₽/сут = {save*365:.0f} ₽/год на устройство")
