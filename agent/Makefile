# edgectx — сборка и проверки.
# Бинарники статические (CGO_ENABLED=0): SQLite — modernc.org/sqlite на чистом Go.

GO       ?= go
VERSION  ?= $(shell git -C .. describe --always --dirty 2>/dev/null || echo dev)
LDFLAGS  := -s -w -X main.version=$(VERSION)
GOFLAGS  := -trimpath
PKG      := ./cmd/edgectx

.PHONY: all build-mac build-linux-arm64 check-static test test-race vet fmt clean

all: vet test build-mac build-linux-arm64

## build-mac: нативный бинарник для разработки (macOS arm64)
build-mac:
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 $(GO) build $(GOFLAGS) -ldflags '$(LDFLAGS)' -o bin/edgectx-darwin-arm64 $(PKG)

## build-linux-arm64: статический бинарник для платы (Raspberry Pi 5, Jetson Nano, Lima VM sbc)
build-linux-arm64:
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build $(GOFLAGS) -ldflags '$(LDFLAGS)' -o bin/edgectx-linux-arm64 $(PKG)

## check-static: убедиться, что linux-бинарник статический
check-static: build-linux-arm64
	@file bin/edgectx-linux-arm64
	@file bin/edgectx-linux-arm64 | grep -q 'statically linked' || (echo 'NOT STATIC' && exit 1)

## test: юнит- и интеграционные тесты
test:
	$(GO) test ./...

## test-race: тесты с детектором гонок (на Mac требует cgo, он включён по умолчанию)
test-race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

fmt:
	gofmt -w ./cmd ./internal ./tests

clean:
	rm -rf bin/edgectx bin/edgectx-*
