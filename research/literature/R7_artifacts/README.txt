Вспомогательные файлы к R7_benchmarks_methodology.md (2026-10-05).
- rusage_energy_probe.c  : чтение per-process счётчиков proc_pid_rusage(RUSAGE_INFO_V6): ri_energy_nj, ri_instructions, ri_cycles. Без sudo.
    clang -O1 -o ru rusage_energy_probe.c && ./ru
- ioreport_energy_probe.c: подписка на группу IOReport "Energy Model" (приватный API libIOReport, тот же источник, что у powermetrics/macmon). Без sudo.
    clang -O1 -o ior ioreport_energy_probe.c -framework CoreFoundation -lIOReport && ./ior 0   (./ior 1 = 2 c нагрузки на 1 ядро)
    Наблюдение на M4 Pro/macOS 27: канал "GPU Energy" (nJ) отдаёт ненулевую дельту; каналы CPU (mJ) в наивной схеме дельты дали 0 — требуется доработка (см. заметку).
- stats_tables.py        : расчёт таблиц CI Уилсона, числа запросов MLPerf и MDE парного сравнения.
    python3 stats_tables.py
Battery-телеметрия (без sudo): ioreg -rn AppleSmartBattery | grep -E 'Voltage|InstantAmperage|PowerTelemetryData'
