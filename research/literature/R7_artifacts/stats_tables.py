# Расчёты, использованные в R7_benchmarks_methodology.md (воспроизводимо, только стандартная библиотека Python).
# 1) Полуширина 95% доверительного интервала Уилсона для доли (точности) при размере категории n.
# 2) Число запросов по формуле MLPerf Inference (Reddi et al., ISCA 2020, ур. (1)-(2)): доверие 99%, margin=(1-tail)/20.
# 3) Минимально обнаружимая разность (MDE) точности двух моделей в парном дизайне (McNemar, нормальное приближение Connor 1987),
#    мощность 0.8, alpha=0.05, d = доля дискордантных пар.
import math
from statistics import NormalDist
z = NormalDist().inv_cdf

def wilson(p, n, a=0.05):
    zz = z(1 - a / 2); d = 1 + zz * zz / n
    c = (p + zz * zz / (2 * n)) / d
    h = zz * math.sqrt(p * (1 - p) / n + zz * zz / (4 * n * n)) / d
    return c - h, c + h

print("Wilson 95% CI half-width, п.п.; столбцы p = 0.5, 0.7, 0.8, 0.9, 0.95")
for n in [16, 24, 100, 200, 240, 400, 1000, 3652]:
    print(n, [round(50 * (wilson(p, n)[1] - wilson(p, n)[0]), 1) for p in (0.5, 0.7, 0.8, 0.9, 0.95)])

for tl in (0.90, 0.95, 0.97, 0.99):
    m = (1 - tl) / 20
    N = (z(1 - 0.01 / 2)) ** 2 * tl * (1 - tl) / m ** 2
    print("MLPerf: tail", tl, "-> queries", round(N))

def mde(n, d, alpha=0.05, power=0.8):
    za, zb = z(1 - alpha / 2), z(power)
    lo, hi = 1e-4, d
    for _ in range(100):
        mid = (lo + hi) / 2
        need = ((za * math.sqrt(d) + zb * math.sqrt(max(d - mid * mid, 1e-12))) ** 2) / (mid * mid)
        lo, hi = (mid, hi) if need > n else (lo, mid)
    return hi

print("Paired MDE, п.п.; столбцы d = 0.05, 0.10, 0.20")
for n in [200, 400, 1000, 2000, 3652]:
    print(n, [round(100 * mde(n, d), 1) for d in (0.05, 0.1, 0.2)])
