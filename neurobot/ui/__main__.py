"""Пульт оператора NeuroChip: сборка бандла «Записи», раздача «Записи» и живой шлюз (README).

  python -m neurobot.ui replay --runs DIR [ID=DIR …] --out bundle.json.gz
  python -m neurobot.ui serve --bundle FILE [--port 18120]
  python -m neurobot.ui live [--env FILE] [--port N] [--state-dir DIR]     живой шлюз (юнит neurobot-ui, ROS 2)
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple

PORT = 18120     # вне занятых диапазонов: EdgeCtx 18080–18089, MCP робота 18110–18119, PicoClaw 18790 (U2 §5)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def deploy_on_path() -> None:
    """neurobot_deploy (env-файл, systemctl) — в <prefix>/lib на роботе или в neurobot/deploy репозитория."""
    try:
        import neurobot_deploy  # noqa: F401
        return
    except ImportError:
        pass
    for d in (os.path.join(os.environ.get("NEUROBOT_PREFIX", "/opt/neurobot"), "lib"),
              os.path.join(ROOT, "neurobot", "deploy")):
        if os.path.isdir(os.path.join(d, "neurobot_deploy")):
            sys.path.insert(0, d)
            return


def run_specs(items: List[str]) -> List[Tuple[Optional[str], str]]:
    """DIR или ID=DIR; ID — латиница, цифры, _ и - (по нему пульт выбирает прогон)."""
    out = []
    for item in items:
        rid, sep, path = item.partition("=")
        if not sep:
            rid, path = None, item
        elif not rid or not all(c.isalnum() or c in "_-" for c in rid):
            raise SystemExit("--runs: ID=DIR, ID из латиницы и цифр: %r" % item)
        if not os.path.isdir(path):
            raise SystemExit("--runs: нет каталога %s" % path)
        out.append((rid, path))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["live"]:          # свой разбор аргументов; rclpy — только здесь
        deploy_on_path()
        from neurobot.ui import node
        return node.main(argv[1:])
    p = argparse.ArgumentParser(prog="python -m neurobot.ui", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("replay", help="собрать бандл «Записи» из каталогов прогонов на двойнике")
    r.add_argument("--runs", nargs="+", required=True, metavar="[ID=]DIR",
                   help="каталоги прогонов runner.py (run.json, safety.jsonl, events.jsonl, …)")
    r.add_argument("--out", required=True, help="файл бандла (.json или .json.gz)")
    r.add_argument("--note", default="", help="подпись бандла (по умолчанию — «стенд, ориентир»)")
    s = sub.add_parser("serve", help="раздать пульт и бандл на 127.0.0.1")
    s.add_argument("--bundle", required=True, help="файл бандла")
    s.add_argument("--port", type=int, default=PORT, help="порт (по умолчанию %d)" % PORT)
    s.add_argument("--host", default="127.0.0.1", help="127.0.0.1 или ::1 — другого нет")
    sub.add_parser("live", help="живой шлюз: узел rclpy и HTTP на 127.0.0.1 (юнит neurobot-ui)")
    a = p.parse_args(argv)
    if a.cmd == "replay":
        from neurobot.ui import replay, stream
        bundle = replay.build(run_specs(a.runs), a.note)
        n = stream.check_bundle(bundle)
        size = stream.write_bundle(a.out, bundle)
        msgs = sum(len(x["stream"]) for x in bundle["runs"])
        print("бандл %s: прогонов %d (%s), сообщений %d, %d КиБ" % (a.out, len(n), ", ".join(n), msgs, size // 1024))
        return 0
    if a.cmd == "serve":
        from neurobot.ui import server
        return server.serve(a.bundle, a.port, a.host)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
