# sysmon — сэмплер ресурсов. Все цели запускаются из каталога sysmon/.
GO      ?= go
VM      ?= sbc
LDFLAGS := -s -w
BIN     := bin

.PHONY: all build-mac build-linux-arm64 build-linux-amd64 test race vet fmt-check vm-test vm-smoke clean

all: vet test build-mac build-linux-arm64

build-mac:
	$(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN)/sysmon-darwin-arm64 ./cmd/sysmon

# Статический бинарник для платы (Raspberry Pi 5, Jetson Nano: ядро 4.9 / glibc 2.27 — статике всё равно).
build-linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN)/sysmon-linux-arm64 ./cmd/sysmon

build-linux-amd64:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN)/sysmon-linux-amd64 ./cmd/sysmon

test:
	$(GO) test ./...

race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...
	GOOS=linux GOARCH=arm64 $(GO) vet ./...

fmt-check:
	@test -z "$$(gofmt -l .)" || (gofmt -l . && exit 1)

# Тесты на настоящем Linux (/proc, cgroup v2) в Lima VM: тестовые бинарники собираются здесь, запускаются там.
vm-test:
	VM=$(VM) scripts/vm_test.sh

# Прогон бинарника в VM на рабочей нагрузке в cgroup с memory.max = 256 МиБ.
vm-smoke: build-linux-arm64
	VM=$(VM) scripts/vm_smoke.sh

clean:
	rm -rf $(BIN)
