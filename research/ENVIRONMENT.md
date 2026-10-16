# Окружение и первичные замеры (2026-10-05)

## Аппаратура
- Apple M4 Pro: 12 ядер CPU (8 производительных + 4 энергоэффективных), встроенный GPU (Metal), Neural Engine.
- 24 ГиБ unified memory, страница 16 КиБ, кэш-линия 128 Б, L2 4 МиБ (по `sysctl`).
- macOS 27.0.1 (arm64). sudo без пароля нет → `powermetrics` (энергия) недоступен агенту; пользователь может запустить сам.
- Диск: ~144 ГиБ свободно.

## ПО
- Apple clang 21 (C++17/20), CMake 4.3.1, Ninja, Homebrew, Node.
- llama.cpp из Homebrew: version 0.5.0 (build 11146, commit 7fe450e19), ggml 0.25.3; backends BLAS + Metal + CPU (apple_m4).
  - Заголовки `/opt/homebrew/opt/llama.cpp/include/llama.h`; есть `llama_state_seq_get/set_data`, `llama_state_seq_save/load_file`,
    `llama_memory_seq_rm/cp`, `llama_sampler_init_grammar`, `llama_get_logits_ith`.
  - Для воспроизводимости ВКР лучше собрать llama.cpp из исходников с зафиксированным коммитом (brew может обновиться).
- Python 3.12.14 + NumPy 2.3.5 (bundled runtime прошлого агента); Homebrew Python 3.14 без NumPy.
- Локальная модель: `/Users/chosenundead/Projects/rudojo/models/Qwen3-8B-Q4_K_M.gguf` (4.68 ГиБ, 8.19 B параметров).

## Данные
- BFCL (Berkeley Function Calling Leaderboard) v3 на Hugging Face `gorilla-llm/Berkeley-Function-Calling-Leaderboard`,
  лицензия Apache-2.0; категории simple/multiple/parallel/parallel_multiple/irrelevance/live_*/multi_turn_*, есть possible_answer.

## Первичный замер llama-bench (Qwen3-8B Q4_K_M), не научный эксперимент — оценка порядка величин
| Режим | pp512 (ток/с) | pp2048 (ток/с) | tg (ток/с) |
|---|---:|---:|---:|
| Metal (GPU), 3 повтора | 338.8 ± 7.0 | 352.4 ± 2.4 | 44.6 ± 0.3 (tg128) |
| CPU only (-ngl 0, 8 потоков), 2 повтора | 38.5 ± 7.0 | — | 24.0 ± 3.4 (tg64) |

Вывод для постановки: промпт агента с каталогом инструментов ~1500 токенов на 8B-модели стоит ~4.3 с prefill на GPU,
то есть TTFT доминирует prefill статической части промпта → переиспользование KV-кэша и сокращение контекста
имеют большой потенциальный эффект; генерация вызова (~40 токенов) ≈ 0.9 с.
