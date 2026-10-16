"""Где запускается аналог: на Mac (LocalExec) или в Lima VM «sbc» (LimaExec, эмуляция платы).

Сервисы стенда (mock-llm, llmtap, sim-mcp, MQTT-брокер) всегда на Mac: внешняя LLM и эмулятор помещения
не на плате (THESIS_CONCEPT §12.3). Агент в ВМ обращается к ним по host.lima.internal (Lima проксирует на
127.0.0.1 хоста — проверено для rosbridge, DECISIONS [robot-mcp]). Каталог проекта в ВМ смонтирован только
для чтения, поэтому рабочие файлы агента в ВМ — в ~/baselines/<run_id>/, после прогона копируются на Mac.
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Proc:
    pid: int
    argv: list[str]
    log: str
    popen: subprocess.Popen | None = None
    meta: dict = field(default_factory=dict)


class LocalExec:
    name = "mac"
    service_host = "127.0.0.1"      # как агент видит сервисы стенда

    def __init__(self) -> None:
        self.procs: list[Proc] = []

    def workdir(self, run_dir: Path) -> str:
        return str(run_dir / "agent")        # рабочий каталог агента (home, workspace, снимки бинарников)

    def mkdir(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def cleanup(self, workdir: str) -> None:
        """Убрать снимки бинарников (SHA — в manifest), остальное оставить в каталоге прогона."""
        shutil.rmtree(Path(workdir) / "bin", ignore_errors=True)

    def write_file(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        if mode is not None:
            p.chmod(mode)

    def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def run(self, argv: list[str], env: dict | None = None, cwd: str | None = None, timeout: float = 120,
            input: bytes | None = None) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run(argv, env=e, cwd=cwd, capture_output=True, timeout=timeout, input=input)

    def spawn(self, argv: list[str], *, log: str, env: dict | None = None, cwd: str | None = None) -> Proc:
        e = dict(os.environ)
        e.update(env or {})
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "ab")
        po = subprocess.Popen(argv, env=e, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              start_new_session=True)
        f.close()
        p = Proc(po.pid, argv, log, po)
        self.procs.append(p)
        return p

    def popen_pipe(self, argv: list[str], *, env: dict | None = None, cwd: str | None = None,
                   stderr_log: str | None = None) -> subprocess.Popen:
        e = dict(os.environ)
        e.update(env or {})
        err = open(stderr_log, "ab") if stderr_log else subprocess.DEVNULL
        return subprocess.Popen(argv, env=e, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                start_new_session=True)

    def alive(self, p: Proc) -> bool:
        if p.popen is not None:
            return p.popen.poll() is None
        try:
            os.kill(p.pid, 0)
            return True
        except OSError:
            return False

    def stop(self, p: Proc, timeout: float = 10.0) -> int | None:
        """SIGTERM своему процессу (по PID), затем SIGKILL, если не завершился."""
        if not self.alive(p):
            return p.popen.returncode if p.popen else None
        try:
            os.kill(p.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive(p):
                break
            time.sleep(0.1)
        if self.alive(p):
            try:
                os.kill(p.pid, signal.SIGKILL)
            except OSError:
                pass
        if p.popen is not None:
            try:
                return p.popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return None
        return None

    def fetch(self, remote: str, local: Path) -> bool:
        if not Path(remote).exists():
            return False
        if Path(remote).resolve() != local.resolve():
            local.parent.mkdir(parents=True, exist_ok=True)
            if Path(remote).is_dir():
                shutil.copytree(remote, local, dirs_exist_ok=True)
            else:
                shutil.copy2(remote, local)
        return True

    def uname(self) -> str:
        return " ".join(os.uname())


class LimaExec:
    """Команды в Lima VM через `limactl shell`. PID — внутри ВМ, остановка — `kill` в ВМ."""

    service_host = "host.lima.internal"

    def __init__(self, vm: str = "sbc"):
        self.vm = vm
        self.name = vm
        self.procs: list[Proc] = []
        self._home: str | None = None

    def _sh(self, script: str, timeout: float = 120, input: bytes | None = None) -> subprocess.CompletedProcess:
        # stdin — /dev/null (или данные): иначе limactl может выделить tty, и сессия ssh ждёт фоновых процессов
        kw = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(["limactl", "shell", "--workdir", "/", self.vm, "--", "bash", "-c", script],
                              capture_output=True, timeout=timeout, **kw)

    def home(self) -> str:
        if self._home is None:
            r = self._sh('printf %s "$HOME"')
            self._home = r.stdout.decode().strip() or "/tmp"
        return self._home

    def workdir(self, run_dir: Path) -> str:
        return f"{self.home()}/baselines/{run_dir.name}"

    def mkdir(self, path: str) -> None:
        self._sh(f"mkdir -p {shlex.quote(path)}")

    def cleanup(self, workdir: str) -> None:
        """Рабочий каталог в ВМ — целиком в архив каталога прогона (без бинарников), затем удалить из ВМ."""
        if not workdir.startswith(f"{self.home()}/baselines/"):
            return
        self._sh(f"rm -rf {shlex.quote(workdir)}/bin")

    def write_file(self, path: str, data: bytes | str, mode: int | None = None) -> None:
        b = data.encode("utf-8") if isinstance(data, str) else data
        chmod = f" && chmod {mode:o} {shlex.quote(path)}" if mode is not None else ""
        r = self._sh(f"mkdir -p {shlex.quote(str(Path(path).parent))} && cat > {shlex.quote(path)}{chmod}", input=b)
        if r.returncode != 0:
            raise RuntimeError(f"write {path} in {self.vm}: {r.stderr.decode()[:400]}")

    def read_file(self, path: str) -> bytes:
        r = self._sh(f"cat {shlex.quote(path)}")
        if r.returncode != 0:
            raise FileNotFoundError(path)
        return r.stdout

    def exists(self, path: str) -> bool:
        return self._sh(f"test -e {shlex.quote(path)}").returncode == 0

    @staticmethod
    def _env_prefix(env: dict | None) -> str:
        return " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())

    def run(self, argv: list[str], env: dict | None = None, cwd: str | None = None, timeout: float = 120,
            input: bytes | None = None) -> subprocess.CompletedProcess:
        cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
        envp = self._env_prefix(env)
        return self._sh(f"{cd}{'env ' + envp + ' ' if envp else ''}{shlex.join(argv)}", timeout=timeout, input=input)

    def spawn(self, argv: list[str], *, log: str, env: dict | None = None, cwd: str | None = None) -> Proc:
        cd = f" && cd {shlex.quote(cwd)}" if cwd else ""
        envp = self._env_prefix(env)
        # подготовка — отдельно (иначе `a && b & echo $!` уводит в фон весь список в подоболочке: сессия ssh
        # ждёт её, а $! — PID подоболочки); в фон — только сама команда, setsid/nohup/env делают exec
        script = (f"mkdir -p {shlex.quote(str(Path(log).parent))}{cd} || exit 97; "
                  f"setsid nohup {'env ' + envp + ' ' if envp else ''}{shlex.join(argv)} >> {shlex.quote(log)} 2>&1 "
                  f"< /dev/null & echo $!")
        r = self._sh(script)
        if r.returncode != 0:
            raise RuntimeError(f"spawn in {self.vm} failed: {r.stderr.decode()[:400]}")
        pid = int(r.stdout.decode().strip().splitlines()[-1])
        p = Proc(pid, argv, log)
        self.procs.append(p)
        return p

    def popen_pipe(self, argv: list[str], *, env: dict | None = None, cwd: str | None = None,
                   stderr_log: str | None = None) -> subprocess.Popen:
        cd = f"cd {shlex.quote(cwd)} && " if cwd else ""
        envp = self._env_prefix(env)
        script = f"{cd}exec {'env ' + envp + ' ' if envp else ''}{shlex.join(argv)}"
        err = open(stderr_log, "ab") if stderr_log else subprocess.DEVNULL
        return subprocess.Popen(["limactl", "shell", "--workdir", "/", self.vm, "--", "bash", "-c", script],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, start_new_session=True)

    def alive(self, p: Proc) -> bool:
        return self._sh(f"kill -0 {p.pid} 2>/dev/null").returncode == 0

    def stop(self, p: Proc, timeout: float = 10.0) -> int | None:
        self._sh(f"kill -TERM {p.pid} 2>/dev/null; for i in $(seq 1 {int(timeout * 10)}); do "
                 f"kill -0 {p.pid} 2>/dev/null || exit 0; sleep 0.1; done; kill -KILL {p.pid} 2>/dev/null; exit 0",
                 timeout=timeout + 30)
        return None

    def fetch(self, remote: str, local: Path) -> bool:
        local.parent.mkdir(parents=True, exist_ok=True)
        r = self._sh(f"test -d {shlex.quote(remote)} && tar -C {shlex.quote(str(Path(remote).parent))} -cf - "
                     f"{shlex.quote(Path(remote).name)} || cat {shlex.quote(remote)}", timeout=300)
        if r.returncode != 0:
            return False
        if self._sh(f"test -d {shlex.quote(remote)}").returncode == 0:
            import io
            import tarfile

            with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
                tf.extractall(local.parent, filter="data")
            src = local.parent / Path(remote).name
            if src != local and src.exists():
                if local.exists():
                    shutil.rmtree(local)
                src.rename(local)
        else:
            local.write_bytes(r.stdout)
        return True

    def uname(self) -> str:
        return self._sh("uname -a").stdout.decode().strip()


def make_executor(host: str):
    if host in ("mac", "local"):
        return LocalExec()
    return LimaExec(host)
