#!/usr/bin/env python3
"""Hook determinista anti-alucinación.

Tras cada Edit/Write/MultiEdit, verifica el archivo editado con las herramientas
REALES disponibles (sintaxis + análisis). No puede alucinar: o el archivo pasa, o
no. Si encuentra errores reales, escribe el detalle en stderr y sale con código 2,
así Claude recibe el error y lo corrige (la edición YA se aplicó; esto no la borra).

Filosofía: NUNCA bloquear por falta de tooling. Si una herramienta no está
instalada o falla por motivos de entorno (no por el código), se salta en silencio.
Un guardarraíl en el que se confía y queda encendido vale más que uno potente que
se termina apagando por ruidoso.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys

# Ubicaciones comunes de herramientas que el PATH con que Claude Code lanza los
# hooks puede NO incluir (p.ej. ~/flutter/bin lo agrega .zshrc; node vive en nvm).
# Sin esto, el hook se saltaría en silencio dart/node/ruff = falsa seguridad.
_EXTRA_PATHS = [
    os.path.expanduser("~/flutter/bin"),
    os.path.expanduser("~/fvm/default/bin"),
    os.path.expanduser("~/.pub-cache/bin"),
    os.path.expanduser("~/.local/bin"),  # uv / uvx
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
] + sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)


def _enriched_path():
    extra = [p for p in _EXTRA_PATHS if os.path.isdir(p)]
    return os.pathsep.join(extra + [os.environ.get("PATH", "")])


def run(cmd, cwd=None, timeout=90):
    """Devuelve (returncode, salida). returncode None = herramienta ausente/timeout.
    Resuelve cmd[0] a ruta absoluta vía un PATH enriquecido para no depender del
    PATH heredado (que al ejecutar hooks suele venir mínimo)."""
    path = _enriched_path()
    exe = shutil.which(cmd[0], path=path)
    if exe is None:
        return None, ""  # herramienta ausente -> saltar (nunca bloquear por tooling)
    env = dict(os.environ)
    env["PATH"] = path
    try:
        p = subprocess.run([exe] + cmd[1:], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, ""


def find_up(start, name):
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, name)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def check(path):
    """Devuelve lista de mensajes de error (vacía = ok)."""
    ext = os.path.splitext(path)[1].lower()
    d = os.path.dirname(path)
    errors = []

    if ext in (".py", ".pyi"):
        # 1) Sintaxis — stdlib, siempre disponible, cero falsos positivos.
        rc, out = run([sys.executable, "-m", "py_compile", path])
        if rc not in (0, None):
            errors.append("SyntaxError (py_compile):\n" + out)
        else:
            # 2) Nombres indefinidos / imports rotos (lo que delata alucinaciones).
            #    ruff vía uvx: rc==1 => hallazgos; rc None/2 => uv ausente o problema
            #    de entorno (run() resuelve uvx por PATH enriquecido; si falta, salta).
            rc, out = run([
                "uvx", "ruff", "check", "--quiet", "--output-format", "concise",
                "--select", "F821,F811,F823", path,
            ], timeout=120)
            if rc == 1 and ".py:" in out:
                errors.append("ruff (nombres indefinidos / imports):\n" + out)

    elif ext in (".js", ".jsx", ".mjs", ".cjs"):
        rc, out = run(["node", "--check", path])
        if rc not in (0, None):
            errors.append("SyntaxError (node --check):\n" + out)

    elif ext in (".ts", ".tsx"):
        root = find_up(d, "node_modules")
        eslint = os.path.join(root, "node_modules", ".bin", "eslint") if root else None
        if eslint and os.path.exists(eslint):
            rc, out = run([eslint, "--no-error-on-unmatched-pattern", path], cwd=root, timeout=120)
            if rc == 1 and out:
                errors.append("eslint:\n" + out)

    elif ext == ".dart":
        # --no-fatal-warnings: rc!=0 solo ante ERRORES reales (infos/warnings no bloquean;
        # los infos ya son no-fatales por defecto). "Usage:" => error de CLI, no de código: saltar.
        rc, out = run(["dart", "analyze", "--no-fatal-warnings", path], timeout=150)
        if rc not in (0, None) and out and "Usage:" not in out:
            errors.append("dart analyze:\n" + out)

    return errors


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    ti = data.get("tool_input", {}) or {}
    path = ti.get("file_path") or ti.get("path")
    if not path or not os.path.isfile(path):
        sys.exit(0)

    errors = check(path)
    if errors:
        sys.stderr.write(
            "⛔ Verificación determinista falló en " + path + ":\n\n"
            + "\n\n".join(errors)
            + "\n\nEstos son errores reales del código recién escrito. "
            "Corrígelos antes de continuar.\n"
        )
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
