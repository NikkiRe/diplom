package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/local/sysmon/internal/testfs"
)

var fixture = filepath.Join("..", "..", "testdata", "real_ubuntu2404_arm64")

func readJSONL(t *testing.T, path string) []map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var recs []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("не JSON: %q: %v", line, err)
		}
		recs = append(recs, m)
	}
	return recs
}

func TestParseFlagsValidation(t *testing.T) {
	bad := [][]string{
		{},                                    // нет цели
		{"--pid", "1", "--interval", "500ms"}, // период < 1 с
		{"--pid", "1", "--interval", "31s"},   // период > 30 с
		{"--pid", "1", "--", "sleep", "1"},    // --pid и команда
		{"--pid", "0"},
		{"--pid", "x"},
		{"--cgroup", "auto"}, // auto без корня
		{"--pid", "1", "--backend", "ps", "--cgroup", "/sys/fs/cgroup/x"},
		{"--pid", "1", "--backend", "nope"},
		{"--pid", "1", "--count", "-1"},
	}
	for _, args := range bad {
		if _, err := parseFlags(args, &bytes.Buffer{}); err == nil {
			t.Errorf("%v: ожидалась ошибка", args)
		}
	}
	o, err := parseFlags([]string{"--pid", "5,6", "--pid", "7", "--interval", "1s", "--iface", "eth0,wlan0", "--backend", "procfs"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if len(o.pids) != 3 || o.pids[2] != 7 || len(o.ifaces) != 2 || o.interval.Seconds() != 1 || !o.perProc {
		t.Fatalf("%+v", o)
	}
	o, err = parseFlags([]string{"--interval", "30s", "--", "edgectx", "--config", "x.yaml"}, &bytes.Buffer{})
	if err != nil || len(o.cmd) != 3 || o.cmd[1] != "--config" {
		t.Fatalf("команда: %+v %v", o, err)
	}
}

func TestRunOnFixture(t *testing.T) {
	out := filepath.Join(t.TempDir(), "run", "sysmon.jsonl")
	var stdout, stderr bytes.Buffer
	code := run([]string{"--backend", "procfs", "--proc-root", filepath.Join(fixture, "proc"),
		"--sys-root", filepath.Join(fixture, "sys"), "--cgroup-root", filepath.Join(fixture, "sys", "fs", "cgroup"),
		"--pid", "4540", "--cgroup", "auto", "--count", "2", "--interval", "1s", "--system", "--clk-tck", "100",
		"--out", out}, &stdout, &stderr)
	if code != exitOK {
		t.Fatalf("код %d: %s", code, stderr.String())
	}
	recs := readJSONL(t, out)
	if len(recs) != 4 {
		t.Fatalf("строк %d", len(recs))
	}
	h := recs[0]
	target := h["target"].(map[string]any)
	if h["type"] != "header" || h["backend"] != "procfs" || h["clk_tck"].(float64) != 100 ||
		!strings.HasSuffix(target["cgroup"].(string), "sysmon-fx-14017.scope") {
		t.Fatalf("header: %v", h)
	}
	s := recs[2]
	tree := s["tree"].(map[string]any)
	cg := s["cgroup"].(map[string]any)
	if tree["procs"].(float64) != 3 || cg["memory_max_kib"].(float64) != 262144 || s["system"] == nil ||
		s["root"].(map[string]any)["procs"].(float64) != 1 || s["dt_s"].(float64) < 0.9 {
		t.Fatalf("sample: %v", s)
	}
	if e := recs[3]; e["type"] != "end" || e["reason"] != "count" || e["samples"].(float64) != 2 {
		t.Fatalf("end: %v", e)
	}
}

func TestRunCommandPropagatesExitCode(t *testing.T) {
	out := filepath.Join(t.TempDir(), "cmd.jsonl")
	var stdout, stderr bytes.Buffer
	code := run([]string{"--interval", "1s", "--out", out, "--", "sh", "-c", "sleep 0.3; exit 3"}, &stdout, &stderr)
	if code != 3 {
		t.Fatalf("код %d (ожидался код команды 3): %s", code, stderr.String())
	}
	recs := readJSONL(t, out)
	end := recs[len(recs)-1]
	if end["type"] != "end" || (end["reason"] != "cmd_exited" && end["reason"] != "target_exited") {
		t.Fatalf("end: %v", end)
	}
	if end["reason"] == "cmd_exited" && end["exit_code"].(float64) != 3 {
		t.Fatalf("exit_code: %v", end)
	}
	first := recs[1]
	if first["type"] != "sample" || first["tree"].(map[string]any)["procs"].(float64) < 1 {
		t.Fatalf("первый сэмпл должен застать команду: %v", first)
	}
}

func TestRunUsageAndVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := run([]string{"--interval", "100ms", "--pid", "1"}, &stdout, &stderr); code != exitUsage {
		t.Fatalf("код %d", code)
	}
	stdout.Reset()
	if code := run([]string{"--version"}, &stdout, &stderr); code != exitOK || !strings.Contains(stdout.String(), version) {
		t.Fatalf("версия: %d %q", code, stdout.String())
	}
	stderr.Reset()
	code := run([]string{"--backend", "procfs", "--proc-root", t.TempDir(), "--pid", "1", "--cgroup", "auto", "--interval", "1s"}, &stdout, &stderr)
	if code != exitError || !strings.Contains(stderr.String(), "--cgroup auto") {
		t.Fatalf("auto без /proc/<pid>/cgroup: %d %q", code, stderr.String())
	}
}

func TestAutoCgroupRejectsSamplerInNestedGroup(t *testing.T) {
	root := t.TempDir()
	procRoot, cgRoot := filepath.Join(root, "proc"), filepath.Join(root, "cgroup")
	testfs.WriteFile(t, filepath.Join(procRoot, "10", "cgroup"), "0::/shared\n")
	testfs.WriteFile(t, filepath.Join(cgRoot, "shared", "cgroup.procs"), "10\n")
	testfs.WriteFile(t, filepath.Join(cgRoot, "shared", "sampler", "cgroup.procs"), strconv.Itoa(os.Getpid())+"\n")
	o, err := parseFlags([]string{"--pid", "10", "--backend", "procfs", "--proc-root", procRoot,
		"--cgroup-root", cgRoot, "--cgroup", "auto"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err = buildCollector(o); err == nil || !strings.Contains(err.Error(), "группа включает sysmon") {
		t.Fatalf("shared cgroup counters include sysmon: %v", err)
	}
}
