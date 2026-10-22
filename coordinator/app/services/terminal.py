"""Защищённый SSH-шлюз для веб-терминала.

Соединение с сервером устанавливает координатор системным клиентом `ssh` внутри псевдотерминала;
браузер получает только поток текста по WebSocket и ключей не видит. Здесь собрано то, что не
зависит от HTTP: политика команд, маскирование секретов для журнала и мост «процесс ↔ PTY».
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import re
import signal
import struct
import termios
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- политика команд

# Команды повышенного риска. Сверка идёт по введённой строке перед нажатием Enter.
# Это защита от ошибки и от «лишнего», а не граница безопасности: настоящей границей остаётся
# ограниченная системная учётная запись на рабочем сервере (без root и без sudo).
_BOUNDARY = r"(?:^|[;&|(`\n]|\$\()\s*"
_DENY: list[tuple[str, re.Pattern[str]]] = [
    ("rm_recursive_root", re.compile(_BOUNDARY + r"(?:sudo\s+)?rm\s+(?:-\S+\s+)*(?:-\S*[rR]\S*|--recursive)\s+(?:-\S+\s+)*(?:--no-preserve-root\s+)?(?:/|/\*|~|~/|\$HOME|\$\{HOME\}|/\.|/\*\.\*)(?:\s|$)")),
    ("rm_no_preserve_root", re.compile(r"--no-preserve-root")),
    ("filesystem_format", re.compile(_BOUNDARY + r"(?:sudo\s+)?(?:mkfs(?:\.\w+)?|wipefs|fdisk|parted|mkswap)\b")),
    ("raw_disk_write", re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/|>\s*/dev/(?:sd|nvme|vd|hd|xvd)\w*")),
    ("power_control", re.compile(_BOUNDARY + r"(?:sudo\s+)?(?:shutdown|reboot|poweroff|halt)\b|" + _BOUNDARY + r"(?:sudo\s+)?init\s+[06]\b")),
    ("fork_bomb", re.compile(r":\s*\(\s*\)\s*\{")),
    ("privilege_escalation", re.compile(_BOUNDARY + r"(?:sudo|su|doas|pkexec)(?:\s|$)")),
    ("account_management", re.compile(_BOUNDARY + r"(?:passwd|chpasswd|useradd|userdel|usermod|groupadd|visudo|chsh)\b")),
    ("mass_permissions", re.compile(_BOUNDARY + r"chmod\s+(?:-\S+\s+)*-\S*R\S*\s+\S+\s+/(?:\s|$)|" + _BOUNDARY + r"chown\s+(?:-\S+\s+)*-\S*R\S*\s+\S+\s+/(?:\s|$)")),
    ("pipe_to_shell", re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")),
    ("firewall_flush", re.compile(_BOUNDARY + r"(?:sudo\s+)?(?:iptables|ip6tables|nft|ufw)\b[^\n]*(?:-F\b|--flush|flush|disable)")),
    ("kill_everything", re.compile(_BOUNDARY + r"(?:sudo\s+)?kill\s+(?:-\S+\s+)*-1\b|" + _BOUNDARY + r"(?:sudo\s+)?killall5\b")),
]

_SECRET = re.compile(
    r"(?i)((?:pass(?:word|wd)?|token|secret|api[_-]?key)\s*[=:]\s*|(?:bearer|basic)\s+|--password(?:=|\s+)|--token(?:=|\s+))(\S+)"
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def mask_secrets(line: str, limit: int = 300) -> str:
    """Значения, похожие на пароли и токены, заменяются на ***; длинные строки обрезаются."""
    out = _SECRET.sub(lambda m: m.group(1) + "***", line)
    return out if len(out) <= limit else out[:limit] + "…"


def check_command(line: str) -> str | None:
    """Код правила, нарушенного строкой, или None."""
    norm = re.sub(r"\s+", " ", line.strip())
    if not norm:
        return None
    for code, rx in _DENY:
        if rx.search(norm):
            return code
    return None


@dataclass
class Fed:
    """Что делать с порцией ввода: forward — переслать в PTY; executed/blocked — для журнала."""
    forward: str = ""
    executed: list[str] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (код правила, строка)


class CommandPolicy:
    """Разбирает ввод пользователя по строкам и не пропускает запрещённые команды.

    Ведётся копия строки readline с положением курсора (стрелки, Home/End, Delete, Ctrl-A/E/K/U/W).
    Если строка нарушает правило, перед Enter она стирается (Ctrl-E, Ctrl-U) и в оболочку не попадает.
    Что подставят история и автодополнение, неизвестно: такая строка проверяется по набранному,
    а в журнал получает пометку. Пока терминал не печатает ввод (запрос пароля), ввод не разбирается
    и не журналируется.
    """

    ERASE = "\x05\x15"  # в конец строки и стереть всё до начала

    def __init__(self, max_line: int = 4096) -> None:
        self.buf: list[str] = []
        self.pos = 0
        self.opaque = False
        self.max_line = max_line
        self._esc = ""

    def _reset(self) -> None:
        self.buf, self.pos, self.opaque = [], 0, False

    def _escape(self, e: str) -> None:
        """Завершённая управляющая последовательность."""
        if e in ("\x1b[200~", "\x1b[201~"):  # границы вставки из буфера обмена
            return
        final = e[-1]
        if e[1] in "[O" and len(e) == 3 and final in "CD":  # вправо / влево
            self.pos = min(len(self.buf), self.pos + 1) if final == "C" else max(0, self.pos - 1)
        elif e[1] in "[O" and final in "Hh" or e == "\x1b[1~":  # Home
            self.pos = 0
        elif e[1] in "[O" and final == "F" or e == "\x1b[4~":  # End
            self.pos = len(self.buf)
        elif e == "\x1b[3~":  # Delete
            if self.pos < len(self.buf):
                del self.buf[self.pos]
        else:  # вверх/вниз (история), Alt-клавиши и прочее
            self.opaque = True

    def feed(self, data: str, echo: bool = True) -> Fed:
        fed = Fed()
        out: list[str] = []
        for ch in data:
            if self._esc:
                self._esc += ch
                e = self._esc
                if len(e) == 2 and ch in "[O":
                    continue
                done = len(e) == 2 or e[1] == "O" or (e[1] == "[" and "@" <= ch <= "~")
                if not done:
                    continue
                self._esc = ""
                if echo:
                    self._escape(e)
                out.append(e)
                continue
            if ch == "\x1b":
                self._esc = ch
                continue
            if not echo:  # ввод пароля: не разбираем и не храним
                out.append(ch)
                continue
            if ch in ("\r", "\n"):
                line = "".join(self.buf)
                opaque = self.opaque
                self._reset()
                rule = check_command(line)
                if rule:
                    fed.blocked.append((rule, mask_secrets(line)))
                    out.append(self.ERASE + "\r")  # пустая команда: оболочка покажет новое приглашение
                else:
                    if line.strip() or opaque:
                        text = mask_secrets(line)
                        fed.executed.append(f"{text}  [история/автодополнение]" if opaque else text)
                    out.append(ch)
            elif ch in ("\x7f", "\b"):
                if self.pos > 0:
                    del self.buf[self.pos - 1]
                    self.pos -= 1
                out.append(ch)
            elif ch in ("\x03", "\x04", "\x07"):  # Ctrl-C, Ctrl-D, Ctrl-G: строка отменена
                self._reset()
                out.append(ch)
            elif ch == "\x01":
                self.pos = 0
                out.append(ch)
            elif ch == "\x05":
                self.pos = len(self.buf)
                out.append(ch)
            elif ch == "\x02":
                self.pos = max(0, self.pos - 1)
                out.append(ch)
            elif ch == "\x06":
                self.pos = min(len(self.buf), self.pos + 1)
                out.append(ch)
            elif ch == "\x0b":  # Ctrl-K: до конца строки
                del self.buf[self.pos:]
                out.append(ch)
            elif ch == "\x15":  # Ctrl-U: до начала строки
                del self.buf[:self.pos]
                self.pos = 0
                out.append(ch)
            elif ch == "\x17":  # Ctrl-W: предыдущее слово
                i = self.pos
                while i > 0 and self.buf[i - 1] == " ":
                    i -= 1
                while i > 0 and self.buf[i - 1] != " ":
                    i -= 1
                del self.buf[i:self.pos]
                self.pos = i
                out.append(ch)
            elif ch in ("\t", "\x12", "\x10", "\x0e", "\x19", "\x1f", "\x14", "\x0c"):
                # автодополнение, поиск по истории, Ctrl-P/N/Y/_/T: итоговую строку неизвестно
                if ch != "\x0c":
                    self.opaque = True
                out.append(ch)
            elif ch >= " ":
                if len(self.buf) < self.max_line:
                    self.buf.insert(self.pos, ch)
                    self.pos += 1
                out.append(ch)
            else:
                out.append(ch)
        fed.forward = "".join(out)
        return fed


_SECRET_PROMPT = re.compile(r"(?i)(pass(?:word|phrase|code)|пароль|парольная фраза|pin)[^\n]{0,40}:\s*$")


class SecretPromptDetector:
    """Определяет запрос пароля по выводу удалённой стороны («[sudo] password for x:», «Password:»).

    Эхо локального PTY для этого не годится: ssh переводит его в «сырой» режим, а эхо делает удалённый сервер.
    После запроса следующая введённая строка считается секретом: не разбирается и не журналируется.
    """

    def __init__(self) -> None:
        self.tail = ""
        self.active = False

    def feed_output(self, text: str) -> None:
        self.tail = (self.tail + strip_ansi(text))[-200:].split("\n")[-1].split("\r")[-1]
        if _SECRET_PROMPT.search(self.tail):
            self.active = True

    def feed_input(self, data: str) -> None:
        if self.active and ("\r" in data or "\n" in data):
            self.active = False
            self.tail = ""


# --------------------------------------------------------------------------- мост процесс ↔ PTY


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def set_winsize(fd: int, rows: int, cols: int) -> None:
    rows, cols = max(2, min(rows, 500)), max(2, min(cols, 500))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def echo_enabled(fd: int) -> bool:
    """Печатает ли терминал ввод. Выключено при запросе пароля."""
    try:
        return bool(termios.tcgetattr(fd)[3] & termios.ECHO)
    except (termios.error, OSError):
        return True


class PtyProcess:
    """Дочерний процесс на псевдотерминале с асинхронным чтением вывода."""

    def __init__(self, argv: list[str], rows: int = 24, cols: int = 80, env: dict[str, str] | None = None) -> None:
        self.argv = argv
        self.rows, self.cols = rows, cols
        self.env = env if env is not None else {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TERM": "xterm-256color", "LANG": "C.UTF-8"}
        self.master = -1
        self.proc: asyncio.subprocess.Process | None = None
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    async def start(self) -> None:
        master, slave = pty.openpty()
        try:
            set_winsize(slave, self.rows, self.cols)
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True, env=self.env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: у ведомой стороны не осталось процессов
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self.master)
        self.output.put_nowait(data or None)

    def decode(self, data: bytes) -> str:
        return self._decoder.decode(data)

    def write(self, text: str) -> None:
        if self.master < 0 or not text:
            return
        raw = text.encode("utf-8", "replace")
        while raw:
            try:
                n = os.write(self.master, raw)
            except BlockingIOError:
                break  # ввод человека мал; при переполнении лишнее отбрасываем
            except OSError:
                return
            raw = raw[n:]

    def resize(self, rows: int, cols: int) -> None:
        if self.master >= 0:
            try:
                set_winsize(self.master, rows, cols)
                if self.proc and self.proc.returncode is None:
                    os.killpg(self.proc.pid, signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

    @property
    def echo(self) -> bool:
        return echo_enabled(self.master) if self.master >= 0 else True

    def kill(self) -> int | None:
        """Синхронное завершение без ожиданий: годится для очистки, которую нельзя прервать отменой задачи."""
        try:
            loop = asyncio.get_running_loop()
            if self.master >= 0:
                loop.remove_reader(self.master)
        except (ValueError, RuntimeError):
            loop = None
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGHUP)
                if loop is not None:  # если ssh не завершился по SIGHUP — добиваем через пару секунд
                    loop.call_later(2.0, self._force_kill)
            except (ProcessLookupError, PermissionError):
                pass
        if self.master >= 0:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = -1
        return self.proc.returncode if self.proc is not None else None

    def _force_kill(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    async def close(self) -> int | None:
        if self.master >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except (ValueError, RuntimeError):
                pass
        code = None
        if self.proc is not None:
            if self.proc.returncode is None:
                for sig, wait in ((signal.SIGHUP, 1.0), (signal.SIGKILL, 2.0)):
                    try:
                        os.killpg(self.proc.pid, sig)
                    except (ProcessLookupError, PermissionError):
                        break
                    try:
                        await asyncio.wait_for(self.proc.wait(), wait)
                        break
                    except asyncio.TimeoutError:
                        continue
            code = self.proc.returncode
        if self.master >= 0:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = -1
        return code
