#!/usr/bin/env bash
# Установка PicoClaw v0.3.1 (sipeed/picoclaw, MIT) из GitHub releases: darwin-arm64 (Mac) и linux-arm64 (ВМ/плата).
# Идемпотентно; SHA-256 архивов сверяется с закреплёнными значениями И с picoclaw_0.3.1_checksums.txt релиза.
#   bench/baselines/install_picoclaw.sh            # скачать/проверить/распаковать в bench/baselines/vendor/picoclaw/v0.3.1
#   bench/baselines/install_picoclaw.sh --src      # ещё клон исходников на теге (для чтения кода) в vendor/src/picoclaw
# Бинарники в ВМ не копируются: каталог проекта смонтирован в ВМ (только чтение), драйвер копирует бинарник
# в рабочий каталог прогона (~/baselines/<run_id>/bin) при каждом прогоне.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VER=0.3.1
GIT=2cf030d2fd3b871d7ec17e3be34c24688aac76da
DEST="$HERE/vendor/picoclaw/v$VER"
BASE="https://github.com/sipeed/picoclaw/releases/download/v$VER"
# bash 3.2 (macOS) без ассоциативных массивов
want_sha() {
  case "$1" in
    picoclaw_Darwin_arm64.tar.gz) echo 30266e4edd6b0dfb871c6ac58e69ffdf8039fc65f23f2612f8792f704930765b ;;
    picoclaw_Linux_arm64.tar.gz) echo e78ab0de05d000698ff8b32458e71cb3134e52e9eaf540e3323a72e9914de060 ;;
    picoclaw_0.3.1_checksums.txt) echo a60e0242f72508f5fbd8ab5976661d0d183c76d8f7863a383e60f3262e976acf ;;
  esac
}
plat_dir() { case "$1" in *Darwin_arm64*) echo darwin-arm64 ;; *Linux_arm64*) echo linux-arm64 ;; esac; }
sha() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1; }

mkdir -p "$DEST/dl"
for f in picoclaw_0.3.1_checksums.txt picoclaw_Darwin_arm64.tar.gz picoclaw_Linux_arm64.tar.gz; do
  p="$DEST/dl/$f"
  if [[ ! -f "$p" || "$(sha "$p")" != "$(want_sha "$f")" ]]; then
    echo "download $f"
    curl -fsSL -o "$p.part" "$BASE/$f" && mv "$p.part" "$p"
  fi
  got="$(sha "$p")"
  [[ "$got" == "$(want_sha "$f")" ]] || { echo "SHA-256 mismatch for $f: $got" >&2; exit 1; }
done
for f in picoclaw_Darwin_arm64.tar.gz picoclaw_Linux_arm64.tar.gz; do
  grep -q "$(want_sha "$f")  $f" "$DEST/dl/picoclaw_0.3.1_checksums.txt" || { echo "$f not in release checksums" >&2; exit 1; }
  d="$DEST/$(plat_dir "$f")"
  if [[ ! -x "$d/picoclaw" ]]; then
    mkdir -p "$d" && tar -xzf "$DEST/dl/$f" -C "$d"
  fi
  head -1 "$d/LICENSE" | grep -q "MIT License" || { echo "unexpected license in $d/LICENSE" >&2; exit 1; }
done
xattr -c "$DEST/darwin-arm64/picoclaw" 2>/dev/null || true
"$DEST/darwin-arm64/picoclaw" --no-color version 2>/dev/null | grep -q "picoclaw $VER (git: ${GIT:0:8})" \
  || { echo "darwin binary does not report $VER / ${GIT:0:8}" >&2; exit 1; }
file "$DEST/linux-arm64/picoclaw" | grep -q "statically linked" || echo "warning: linux binary is not static?" >&2

python3 - "$DEST" "$VER" "$GIT" <<'EOF'
import hashlib, json, sys, pathlib
dest, ver, git = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
out = {"name": "picoclaw", "version": ver, "git": git, "license": "MIT", "source": "https://github.com/sipeed/picoclaw",
       "release": f"https://github.com/sipeed/picoclaw/releases/tag/v{ver}", "published_at": "2026-07-03T07:37:06Z",
       "assets": {f.name: sha(f) for f in sorted((dest / "dl").glob("*")) if f.is_file()},
       "binaries": {f"{d}/{n}": {"sha256": sha(dest / d / n), "bytes": (dest / d / n).stat().st_size}
                    for d in ("darwin-arm64", "linux-arm64") for n in ("picoclaw", "picoclaw-launcher", "LICENSE")}}
(dest / "versions.json").write_text(json.dumps(out, indent=1) + "\n")
print(json.dumps(out["binaries"], indent=1))
EOF

if [[ "${1:-}" == "--src" ]]; then
  S="$HERE/vendor/src/picoclaw"
  [[ -d "$S/.git" ]] || git clone -q --depth 1 --branch "v$VER" https://github.com/sipeed/picoclaw.git "$S"
  [[ "$(git -C "$S" rev-parse HEAD)" == "$GIT" ]] || { echo "source HEAD != $GIT" >&2; exit 1; }
fi
echo "picoclaw $VER OK in $DEST"
