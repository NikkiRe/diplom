# scripts/board

ВМ-сторона эмуляции «убогой» платы: `board.sh` (netns + netem, расписание обрывов, запуск под лимитами cgroup v2
через systemd, уборка), `netem.jq` (аргументы netem направления профиля), `record.jq` (итог юнита `run`).
Профили — `bench/board/profiles.json`; описание, флаги, решения и результаты проверки — `bench/board/README.md`.

```bash
limactl shell --workdir / sbc -- sudo bash "$PWD/scripts/board/board.sh"   # справка
```
