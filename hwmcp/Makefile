# hw-mcp — MCP-сервер контекста оборудования (Go, статический бинарник).
GO      ?= go
VERSION ?= $(shell date -u +%Y%m%d)-dev
LDFLAGS := -s -w -X main.version=$(VERSION)
BUILD   := .build
VM      ?= sbc

.PHONY: all build build-linux-arm64 test test-short vet bench golden vm-test vm-gpiosim vm-hotplug run-fixture print-tools clean

all: vet test build build-linux-arm64

build:
	CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BUILD)/hw-mcp ./cmd/hw-mcp

# Плата (Raspberry Pi 5 / Jetson Nano: ядро 4.9, glibc 2.27 — статика это покрывает) и ВМ Lima.
build-linux-arm64:
	GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BUILD)/hw-mcp-linux-arm64 ./cmd/hw-mcp
	@file $(BUILD)/hw-mcp-linux-arm64 2>/dev/null || true

test:
	$(GO) test -race ./...

test-short:
	$(GO) test -short ./...

vet:
	$(GO) vet ./...
	GOOS=linux GOARCH=arm64 $(GO) vet ./...
	@test -z "$$(gofmt -l .)" || (gofmt -l .; echo "gofmt: files above need formatting"; exit 1)

bench:
	$(GO) test -run '^$$' -bench . -benchmem ./internal/gate ./internal/argschema ./internal/hub ./internal/sysfs

# Эталоны паритета из Python-прототипов research/ (только чтение research/).
golden:
	cd .. && .venv/bin/python hwmcp/tools/gen_golden.py

# Тесты linux/arm64 в ВМ Lima: пакеты + сквозные тесты с готовым бинарником.
# Проверка настоящего /dev/gpiochip0 — отдельно, см. README (меняет уровни линий, нужен sudo).
vm-test: build-linux-arm64
	@mkdir -p $(BUILD)/vm
	@for p in $$($(GO) list ./... | grep -v testutil); do \
		n=$$(basename $$p); \
		GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) test -c -o $(BUILD)/vm/$$n.test $$p || exit 1; \
	done
	limactl shell $(VM) -- bash -c 'cd $(CURDIR) && for t in $(BUILD)/vm/*.test; do \
		HWMCP_BIN=$(CURDIR)/$(BUILD)/hw-mcp-linux-arm64 ./$$t -test.count=1 >/tmp/hwmcp-vm.log 2>&1 \
		&& echo "ok   $$t" || { echo "FAIL $$t"; tail -30 /tmp/hwmcp-vm.log; exit 1; }; done'

# GPIO на настоящем ядре ВМ: модуль gpio-sim, uAPI v1 и v2, метки чипов, hw-mcp в смешанном режиме (нужен sudo в ВМ).
vm-gpiosim:
	tools/vm_gpiosim_check.sh

# Пропажа и возвращение устройства на настоящем sysfs ВМ: модуль iio_dummy через configfs (нужен sudo в ВМ).
vm-hotplug:
	tools/vm_hotplug_check.sh

# Сервер на копии фикстуры (фикстура в testdata не меняется), streamable HTTP на свободном порту.
run-fixture: build
	@tmp=$$(mktemp -d); cp -a testdata/fixture_root $$tmp/root; \
	echo "fixture copy: $$tmp/root"; \
	$(BUILD)/hw-mcp -root $$tmp/root -declared configs/declared.lab.json -transport http \
		-trace $$tmp/hw-mcp.jsonl -log-level info

print-tools: build
	$(BUILD)/hw-mcp -root testdata/fixture_root -declared configs/declared.lab.json -catalog explicit -print-tools

clean:
	rm -rf $(BUILD)
