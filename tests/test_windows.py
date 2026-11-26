"""Windows support: batch scripts, UTF-8 file I/O, keep-awake and the .env check. These run on any OS;
the Windows-only parts are simulated (there's no Windows machine in the test run)."""
from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path
from types import SimpleNamespace

import config

ROOT = Path(config.ROOT)
SCRIPTS = ROOT / "scripts"


def test_every_shell_script_has_a_windows_batch_file():
    sh = {p.stem for p in SCRIPTS.glob("*.sh")}
    bat = {p.stem for p in SCRIPTS.glob("*.bat")}
    assert sh <= bat and {"check", "run"} <= bat


def test_batch_files_are_crlf_ascii_and_use_the_venv():
    for p in SCRIPTS.glob("*.bat"):
        raw = p.read_bytes()
        raw.decode("ascii")                                              # cmd.exe code pages: keep them ASCII
        assert b"\n" not in raw.replace(b"\r\n", b""), f"{p.name}: LF-only line"
        text = raw.decode("ascii")
        assert text.startswith("@echo off\r\n") and 'cd /d "%~dp0.."' in text, p.name
        assert ".venv\\Scripts\\python.exe" in text and "PYTHONUTF8=1" in text, p.name
        assert ".venv/bin" not in text, p.name
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in attrs and "*.sh  text eol=lf" in attrs


def test_batch_blocks_escape_parentheses_in_echo():
    """Inside ( ... ) blocks an unescaped ")" in an echo ends the block early."""
    for p in SCRIPTS.glob("*.bat"):
        depth = 0
        for n, line in enumerate(p.read_text(encoding="ascii").splitlines(), 1):
            s = line.strip()
            if depth and s.lower().startswith("echo"):
                body = re.sub(r"\^.", "", s)
                assert "(" not in body and ")" not in body, f"{p.name}:{n}: escape parentheses as ^( ^)"
            if s.endswith("(") and not s.lower().startswith(("rem", "echo")):
                depth += 1
            elif s == ")":
                depth -= 1
        assert depth == 0, p.name


def test_all_text_file_io_names_an_encoding():
    """Windows defaults to cp1252, which can't write the rupee sign: every read_text / write_text / open of
    a text file in the app must say encoding="utf-8"."""
    files = [ROOT / f for f in ("app.py", "config.py", "manage.py", "history.py", "backtest.py", "universe.py")]
    files += sorted((ROOT / "screener").glob("*.py"))
    pat = re.compile(r"\.(read_text|write_text)\(|(?<![\w.])open\(|os\.fdopen\(")
    bad = []
    for f in files:
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not pat.search(line) or line.lstrip().startswith(("#", "python -c")) or "def _on_open" in line:
                continue
            stmt = " ".join(lines[i:i + 3])
            if "encoding=" not in stmt and "rb" not in stmt and "wb" not in stmt:
                bad.append(f"{f.name}:{i + 1}: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_keep_awake_on_windows(monkeypatch):
    import app
    calls = []
    fake = SimpleNamespace(SetThreadExecutionState=lambda flags: calls.append(flags) or 1)
    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=fake), raising=False)
    cfg = SimpleNamespace(PREVENT_SLEEP=True)
    awake = app.keep_awake(cfg)
    assert awake is not None and calls == [0x80000001]                  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    awake.terminate()
    assert calls[-1] == 0x80000000                                       # back to normal
    failing = SimpleNamespace(SetThreadExecutionState=lambda flags: 0)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=failing), raising=False)
    assert app.keep_awake(cfg) is None                                    # logged, not fatal
    assert app.keep_awake(SimpleNamespace(PREVENT_SLEEP=False)) is None


def test_env_permission_check_is_skipped_on_windows(monkeypatch, tmp_path):
    import manage

    class WinOS:                                                          # only manage.py sees "nt"
        name = "nt"

        def __getattr__(self, k):
            return getattr(os, k)
    monkeypatch.setattr(manage, "os", WinOS())
    monkeypatch.setattr(manage.config, "ROOT", tmp_path)
    (tmp_path / ".env").write_text("DHAN_CLIENT_ID=\n", encoding="utf-8")
    r = manage.Report()
    manage.check_env(r)
    row = next(x for x in r.items if x[1] == ".env permissions")
    assert row[0] == manage.OK and "not checked on Windows" in row[2]
