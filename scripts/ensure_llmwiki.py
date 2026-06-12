#!/usr/bin/env python3
"""Verifica (e installa se manca) la skill "llm-wiki" sui runtime agente.

Runtime controllati:
1. Hermes (WSL Ubuntu): ~/.hermes/skills/**/llm-wiki* (incl. openclaw-imports/)
   → se manca: git clone in ~/.hermes/skills/llm-wiki-skill
2. OpenClaw (se il binario esiste): `openclaw skills list --json`
   → se manca: `openclaw skills install git:lewislulu/llm-wiki-skill@main`
3. Claude Code (Windows): %USERPROFILE%\\.claude\\skills\\llm-wiki
   → se manca: git clone

Idempotente; stampa un JSON di stato per runtime. Eseguibile sia da
Windows (usa wsl.exe per il punto 1) sia dentro WSL.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

LLMWIKI_REPO = "https://github.com/lewislulu/llm-wiki-skill"


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, "binario non trovato"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def _wsl(cmd: str, timeout: int = 120) -> tuple[int, str]:
    if os.name == "nt":
        return _run(["wsl.exe", "-d", "Ubuntu", "--", "bash", "-lc", cmd],
                    timeout)
    return _run(["bash", "-lc", cmd], timeout)


def ensure_hermes() -> dict:
    code, out = _wsl("ls -d ~/.hermes/skills 2>/dev/null && "
                     "find ~/.hermes/skills -maxdepth 3 -iname '*llm-wiki*' "
                     "-print -quit")
    if code != 0 and "skills" not in out:
        return {"runtime": "hermes", "status": "not_installed",
                "detail": "~/.hermes/skills non trovata (Hermes assente?)"}
    if "llm-wiki" in out:
        return {"runtime": "hermes", "status": "ok", "detail": out.strip().splitlines()[-1]}
    code, out = _wsl(f"git clone --depth 1 {LLMWIKI_REPO} "
                     "~/.hermes/skills/llm-wiki-skill 2>&1", timeout=180)
    if code == 0:
        return {"runtime": "hermes", "status": "installed",
                "detail": "~/.hermes/skills/llm-wiki-skill"}
    return {"runtime": "hermes", "status": "error", "detail": out.strip()[-300:]}


def ensure_openclaw() -> dict:
    exe = shutil.which("openclaw") or shutil.which("openclaw.cmd")
    if not exe:
        code, out = _wsl("command -v openclaw")
        if code != 0:
            return {"runtime": "openclaw", "status": "skipped",
                    "detail": "openclaw non installato"}
        code, out = _wsl("openclaw skills list --json 2>/dev/null")
        if code == 0 and "llm-wiki" in out:
            return {"runtime": "openclaw", "status": "ok",
                    "detail": "skill presente"}
        code, out = _wsl(
            "openclaw skills install git:lewislulu/llm-wiki-skill@main 2>&1",
            timeout=300)
        status = "installed" if code == 0 else "error"
        return {"runtime": "openclaw", "status": status,
                "detail": out.strip()[-300:]}
    code, out = _run([exe, "skills", "list", "--json"])
    if code == 0 and "llm-wiki" in out:
        return {"runtime": "openclaw", "status": "ok", "detail": "skill presente"}
    code, out = _run([exe, "skills", "install",
                      "git:lewislulu/llm-wiki-skill@main"], timeout=300)
    status = "installed" if code == 0 else "error"
    return {"runtime": "openclaw", "status": status, "detail": out.strip()[-300:]}


def ensure_claude_code() -> dict:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    if os.name != "nt":
        # dentro WSL: la home di Claude Code è quella Windows
        code, out = _wsl("cmd.exe /c echo %USERPROFILE% 2>/dev/null")
        if code == 0 and out.strip():
            win_home = out.strip().splitlines()[-1]
            home = Path("/mnt/c") / Path(win_home.replace("\\", "/")).relative_to("C:/")
    target = home / ".claude" / "skills" / "llm-wiki"
    if (target / "SKILL.md").is_file():
        return {"runtime": "claude-code", "status": "ok", "detail": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    code, out = _run(["git", "clone", "--depth", "1", LLMWIKI_REPO,
                      str(target)], timeout=180)
    if code == 0:
        return {"runtime": "claude-code", "status": "installed",
                "detail": str(target)}
    return {"runtime": "claude-code", "status": "error",
            "detail": out.strip()[-300:]}


def main() -> int:
    results = [ensure_hermes(), ensure_openclaw(), ensure_claude_code()]
    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0 if all(r["status"] in ("ok", "installed", "skipped")
                    for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
