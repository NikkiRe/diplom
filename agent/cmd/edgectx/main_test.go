package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFlags(t *testing.T) {
	var out bytes.Buffer
	if code := run([]string{"-version"}, &out); code != 0 || !strings.Contains(out.String(), "edgectx") {
		t.Fatalf("version: %d %q", code, out.String())
	}
	out.Reset()
	if code := run([]string{"-config", "../../configs/example.yaml", "-check-config"}, &out); code != 0 || !strings.Contains(out.String(), "config OK") {
		t.Fatalf("check-config: %d %q", code, out.String())
	}
	bad := filepath.Join(t.TempDir(), "bad.yaml")
	os.WriteFile(bad, []byte("policy: {wake: sometimes}\n"), 0o644)
	out.Reset()
	if code := run([]string{"-config", bad, "-check-config"}, &out); code != 2 || !strings.Contains(out.String(), "policy.wake") {
		t.Fatalf("bad config: %d %q", code, out.String())
	}
	if code := run([]string{"-no-such-flag"}, &out); code != 2 {
		t.Fatalf("unknown flag: %d", code)
	}
}

func TestRunIDFormat(t *testing.T) {
	id := newRunID()
	if !strings.HasPrefix(id, "edgectx-") || len(id) != len("edgectx-20261006-120000-abcdef") {
		t.Fatalf("run id %q", id)
	}
}
