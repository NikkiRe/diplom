#!/usr/bin/env bash
# Запуск go-тестов sysmon на настоящем Linux в Lima VM (по умолчанию "sbc"): тестовые бинарники
# linux/arm64 собираются на хосте в bin/vmtest/, домашний каталог смонтирован в VM (только чтение),
# каждый бинарник запускается в каталоге своего пакета (тесты читают testdata по относительным путям).
set -euo pipefail
cd "$(dirname "$0")/.."
VM=${VM:-sbc}
root=$PWD
rm -rf bin/vmtest && mkdir -p bin/vmtest
pkgs=()
for dir in $(go list -f '{{if .TestGoFiles}}{{.Dir}}{{end}}' ./...); do
  name=$(echo "${dir#$root/}" | tr '/' '_')
  CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go test -c -o "bin/vmtest/$name.test" "./${dir#$root/}"
  pkgs+=("$dir:$name")
done
script='set -e; fail=0'
for p in "${pkgs[@]}"; do
  dir=${p%%:*}; name=${p##*:}
  script+="; echo \"== $name\"; (cd '$dir' && '$root/bin/vmtest/$name.test' -test.count=1 ${TESTFLAGS:-}) || fail=1"
done
script+='; exit $fail'
limactl shell "$VM" -- bash -c "$script"
