"""Private persistence for explicitly added local MCP endpoints, without any RPC."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path

from . import catalog

FORMAT = "edge-mcp-sources/1"
MAX_BYTES = 65536


class SourceState:
    def __init__(self, state_dir=None):
        self.lock = threading.RLock()
        self.path = Path(state_dir).expanduser().resolve() / "mcp-sources.json" if state_dir is not None else None
        self.error = None
        self._path_error = None
        if self.path is not None and any((parent / ".git").exists() for parent in self.path.parents):
            self._path_error = "Настройки MCP следует хранить в приватном каталоге вне Git-репозитория"
            self.error = self._path_error

    @staticmethod
    def _sources(rows):
        if not isinstance(rows, list) or len(rows) > catalog.MAX_SOURCES:
            raise ValueError("Настройки содержат не массив источников или больше 8 endpoint")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Источник должен быть объектом url/label")
            url = catalog.endpoint(row.get("url"))
            label = row.get("label", "")
            # A public catalog snapshot can display the URL as its automatic label.
            # Restoring the empty configured label produces the same automatic label.
            if label == url:
                label = ""
            if not isinstance(label, str) or len(label) > 120:
                raise ValueError("Название источника должно быть строкой до 120 символов")
            label.encode("utf-8")
            if url in seen:
                raise ValueError("Повторный endpoint в сохранённых настройках")
            seen.add(url)
            result.append({"url": url, "label": label})
        return result

    def load(self):
        """Return validated endpoints only; failures preserve bytes and expose error."""
        with self.lock:
            if self.path is None:
                return []
            if self._path_error:
                self.error = self._path_error
                return []
            fd = None
            try:
                if self.path.is_symlink():
                    raise ValueError("Файл настроек MCP не должен быть символьной ссылкой")
                fd = os.open(str(self.path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("Файл настроек MCP не обычный файл или больше 64 KiB")
                raw = os.read(fd, MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("Файл настроек MCP больше 64 KiB")
                document = json.loads(raw.decode("utf-8"))
                if not isinstance(document, dict) or document.get("format") != FORMAT:
                    raise ValueError("Неизвестный формат сохранённых MCP-источников")
                sources = self._sources(document.get("sources"))
                self.error = None
                return sources
            except FileNotFoundError:
                self.error = None
                return []
            except (OSError, ValueError, UnicodeError, RecursionError, catalog.CatalogError) as exc:
                self.error = "Настройки MCP не прочитаны; исходный файл сохранён: " + str(exc)[:400]
                return []
            finally:
                if fd is not None:
                    os.close(fd)

    def save(self, snapshot):
        """None on success/no persistence; readable error on failure, no registry mutation."""
        with self.lock:
            if self.path is None:
                return None
            # Revalidate any current file, including external edits, before replacing it.
            self.load()
            if self.error:
                return self.error
            temporary = None
            try:
                if not isinstance(snapshot, dict):
                    raise ValueError("Ожидается snapshot каталога")
                sources = self._sources(snapshot.get("sources"))
                raw = json.dumps({"format": FORMAT, "sources": sources}, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode("utf-8")
                if len(raw) > MAX_BYTES:
                    raise ValueError("Настройки MCP больше 64 KiB")
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if hasattr(os, "getuid") and self.path.parent.stat().st_uid != os.getuid():
                    raise ValueError("Приватный каталог MCP должен принадлежать текущему пользователю")
                with tempfile.NamedTemporaryFile(dir=str(self.path.parent), prefix=".mcp-sources-", delete=False) as stream:
                    temporary = stream.name
                    os.chmod(temporary, 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, str(self.path))
                self.error = None
                return None
            except (OSError, ValueError, UnicodeError, catalog.CatalogError) as exc:
                self.error = "Настройки MCP не сохранены; прежний файл сохранён: " + str(exc)[:400]
                return self.error
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
