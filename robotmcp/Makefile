# robot-mcp — компактный MCP-адаптер мобильного робота (Go, статический бинарник).
GO      ?= go
VERSION ?= $(shell date -u +%Y%m%d)-dev
LDFLAGS := -s -w -X main.version=$(VERSION)
BUILD   := .build
VM      ?= sbc
CMDS    := robot-mcp robot-probe fake-rosbridge

.PHONY: all build build-linux-arm64 test test-short vet print-tools vm-copy vm-test golden clean

all: vet test build build-linux-arm64

build:
	@for c in $(CMDS); do CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BUILD)/$$c ./cmd/$$c || exit 1; done

# Плата (Raspberry Pi 5 / Jetson Nano: статика покрывает ядро 4.9 и glibc 2.27) и VM Lima sbc.
build-linux-arm64:
	@for c in $(CMDS); do GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) build -trimpath -ldflags '$(LDFLAGS)' \
		-o $(BUILD)/$$c-linux-arm64 ./cmd/$$c || exit 1; done
	@file $(BUILD)/robot-mcp-linux-arm64 2>/dev/null || true

test:
	$(GO) test -race ./...

test-short:
	$(GO) test -short ./...

vet:
	$(GO) vet ./...
	GOOS=linux GOARCH=arm64 $(GO) vet ./...
	@test -z "$$(gofmt -l .)" || (gofmt -l .; echo "gofmt: files above need formatting"; exit 1)

print-tools: build
	$(BUILD)/robot-mcp -print-tools

# Копия бинарников в домашний каталог VM (а не запуск с virtiofs: страницы файла учитываются в PSS одинаково,
# но так это ближе к установке на плату).
vm-copy: build-linux-arm64
	@h=$$(limactl shell --workdir / $(VM) -- printenv HOME); limactl shell --workdir / $(VM) -- mkdir -p $$h/robotmcp; \
	for c in $(CMDS); do limactl copy $(BUILD)/$$c-linux-arm64 $(VM):$$h/robotmcp/$$c || exit 1; done; \
	limactl shell --workdir / $(VM) -- bash -c 'ls -la ~/robotmcp && ~/robotmcp/robot-mcp -version'

# Тесты linux/arm64 в VM: пакеты + сквозные тесты с готовым бинарником (без -race: в VM нет CGO); каждый
# тест запускается из каталога своего пакета (VM видит проект по тому же пути, только чтение).
vm-test: build-linux-arm64
	@mkdir -p $(BUILD)/vm
	@for p in $$($(GO) list -f '{{if or .TestGoFiles .XTestGoFiles}}{{.ImportPath}}{{end}}' ./...); do n=$$(basename $$p); \
		GOOS=linux GOARCH=arm64 CGO_ENABLED=0 $(GO) test -c -o $(BUILD)/vm/$$n.test $$p || exit 1; \
		echo "$$($(GO) list -f '{{.Dir}}' $$p) $$n" ; done > $(BUILD)/vm/list
	limactl shell --workdir / $(VM) -- bash -c 'while read d n; do cd $$d; \
		ROBOTMCP_BIN=$(CURDIR)/$(BUILD)/robot-mcp-linux-arm64 $(CURDIR)/$(BUILD)/vm/$$n.test -test.count=1 -test.short \
		> ~/robotmcp-vm-$$n.log 2>&1 && echo "ok   $$n" || { echo "FAIL $$n"; tail -30 ~/robotmcp-vm-$$n.log; exit 1; }; \
		done < $(CURDIR)/$(BUILD)/vm/list'

# Эталон оценщика токенов (точный o200k на ответах прототипа, ros-mcp и robot-mcp).
golden:
	cd .. && .venv/bin/python robotmcp/tools/gen_token_golden.py --extra $(EXTRA)

clean:
	rm -rf $(BUILD)
