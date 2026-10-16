#!/bin/zsh
# Пилотный подсчёт токенов для G3_gap.md (§1.3). Только токенизатор, GPU не используется.
# Запуск: cd в эту папку; python3 mcp_schema_formats.py; ./run_token_count.sh
M=${M:-/Users/chosenundead/Projects/rudojo/models/Qwen3-8B-Q4_K_M.gguf}
cnt(){ llama-tokenize -m $M -f "$1" --no-bos --show-count --log-disable 2>/dev/null | grep -i "total number" | grep -oE "[0-9]+"; }
echo "header+footer (шаблон Qwen3 <tools>): $(cnt hdr.txt)"
for i in 0 1 2 3 4 5; do echo "tool$i json=$(cnt t${i}_json.txt) min=$(cnt t${i}_min.txt) sig=$(cnt t${i}_sig.txt) name=$(cnt t${i}_name.txt)"; done
