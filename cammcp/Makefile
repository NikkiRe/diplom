# cam-mcp — MCP-сервер камеры с детектором движения на плате (Go, статический бинарник).
GO      ?= go
VERSION ?= $(shell date -u +%Y%m%d)-dev
LDFLAGS := -s -w -X main.version=$(VERSION)
BUILD   := .build
VM      ?= sbc

.PHONY: all build build-linux-arm64 test test-short vet bench vm-test vm-smoke vm-v4l2 print-tools clean

all: vet test build build-linux-arm64

build:
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BUILD)/cam-mcp ./cmd/cam-mcp

# Плата (Raspberry Pi, Jetson Nano: статика покрывает ядро 4.9 и glibc 2.27) и ВМ Lima.
build-linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BUILD)/cam-mcp-linux-arm64 ./cmd/cam-mcp
	@file $(BUILD)/cam-mcp-linux-arm64 2>/dev/null || true

test:
	$(GO) test -race ./...

test-short:
	$(GO) test -short ./...

vet:
	$(GO) vet ./...
	GOOS=linux GOARCH=arm64 $(GO) vet ./...
	@test -z "$$(gofmt -l .)" || (gofmt -l .; echo "gofmt: files above need formatting"; exit 1)

bench:
	$(GO) test -run '^$$' -bench . -benchmem ./internal/detect ./internal/camera

# Тесты linux/arm64 в ВМ (каталог проекта смонтирован в ВМ только для чтения; без -race — в ВМ нет cgo):
# каждый тестовый бинарник запускается из каталога своего пакета; журнал — во временном файле ВМ.
vm-test:
	@mkdir -p $(BUILD)/vm
	@for p in $$($(GO) list -f '{{if or .TestGoFiles .XTestGoFiles}}{{.ImportPath}}{{end}}' ./...); do n=$$(basename $$p); \
		GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) test -c -o $(BUILD)/vm/$$n.test $$p || exit 1; \
		echo "$$($(GO) list -f '{{.Dir}}' $$p) $$n"; done > $(BUILD)/vm/list
	limactl shell --workdir / $(VM) -- bash -c 'log=$$(mktemp /tmp/cammcp-vm-XXXXXX); trap "rm -f $$log" EXIT; \
		while read d n; do cd $$d; \
		$(CURDIR)/$(BUILD)/vm/$$n.test -test.count=1 > $$log 2>&1 && echo "ok   $$n" \
		|| { echo "FAIL $$n"; tail -30 $$log; exit 1; }; done < $(CURDIR)/$(BUILD)/vm/list'

# Сквозная проверка в ВМ на синтетике под квотой CPU 25 % (около 40 с реального времени).
vm-smoke: build-linux-arm64
	tools/vm_smoke.sh synthetic

# Захват V4L2 в ВМ на виртуальном драйвере ядра vivid (модуль загружается и выгружается; нужен sudo).
vm-v4l2: build-linux-arm64
	@mkdir -p $(BUILD)/vm
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) test -c -o $(BUILD)/vm/source.test ./internal/source
	tools/vm_smoke.sh v4l2

print-tools: build
	$(BUILD)/cam-mcp -print-tools

clean:
	rm -rf $(BUILD)
