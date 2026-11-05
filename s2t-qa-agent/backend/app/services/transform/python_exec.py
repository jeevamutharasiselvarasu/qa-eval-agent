"""PythonExecutionService: static validation + isolated worker process.

Contract (documented in docs/PYTHON_CONTRACT.md):
    def transform(sources: dict[str, pandas.DataFrame]) -> pandas.DataFrame

Defence layers:
  1. AST policy: only allowlisted imports; no eval/exec/open/getattr/...; no dunder/private attribute access;
     no pandas/numpy file or network IO methods.
  2. Separate `python -I` process with empty environment, rlimits (address space, CPU, file size, open files),
     wall-clock timeout and process-group kill.
  3. Inside the worker: restricted builtins, guarded __import__, and a PEP 578 audit hook that blocks
     sockets, subprocesses, os.system/exec/fork, ctypes and any file open outside library code while
     user code runs (the hook cannot be removed).
Production deployments should additionally run the worker in a network-less container (see README).
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ...config import get_settings
from .contract import ExecError

# `operator` (attrgetter/methodcaller) and `string` (Formatter) are excluded: both allow dynamic attribute access.
ALLOWED_IMPORTS = {"pandas", "numpy", "math", "datetime", "decimal", "re", "statistics", "collections",
                   "itertools", "functools"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
                   "setattr", "delattr", "input", "breakpoint", "help", "memoryview", "exit", "quit", "dir",
                   "__builtins__", "__loader__", "__spec__", "classmethod", "staticmethod", "super", "object"}
FORBIDDEN_ATTRS = {"system", "popen", "spawn", "fork", "load", "loads", "save", "savez", "savetxt", "loadtxt",
                   "fromfile", "tofile", "genfromtxt", "memmap", "ctypeslib", "eval", "read_pickle", "to_pickle",
                   "f_globals", "f_locals", "gi_frame", "cr_frame", "tb_frame", "mro", "subclasses",
                   "attrgetter", "methodcaller", "Formatter", "vformat", "format_map", "get_field"}
IO_ATTR_PREFIXES = ("read_", "to_csv", "to_parquet", "to_excel", "to_json", "to_sql", "to_hdf", "to_feather",
                    "to_html", "to_clipboard", "to_latex", "to_markdown", "to_stata", "to_xml", "to_orc", "to_gbq",
                    "to_pickle", "to_records_file", "DataFrame.from_file")
WORKER = Path(__file__).with_name("python_worker.py")


def validate_python(code: str) -> tuple[list[ExecError], list[str]]:
    errors: list[ExecError] = []
    warnings: list[str] = []
    if not code.strip():
        return [ExecError("Code is empty", kind="SYNTAX")], warnings
    if len(code) > 200_000:
        return [ExecError("Code is too long", kind="SAFETY")], warnings
    try:
        tree = ast.parse(code, filename="<transformation>")
    except SyntaxError as e:
        return [ExecError(f"SyntaxError: {e.msg}", e.lineno, e.offset, "SYNTAX")], warnings
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "transform"]
    if not fn:
        errors.append(ExecError("Code must define `def transform(sources):`", kind="CONTRACT"))
    elif len(fn[0].args.args) != 1:
        errors.append(ExecError("transform() must take exactly one argument: sources", fn[0].lineno, kind="CONTRACT"))
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", None)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if (m or "").split(".")[0] not in ALLOWED_IMPORTS or (isinstance(node, ast.ImportFrom) and node.level):
                    errors.append(ExecError(f"Import of '{m}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}", ln, kind="SAFETY"))
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            errors.append(ExecError(f"Use of '{node.id}' is not allowed", ln, kind="SAFETY"))
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("_"):
                errors.append(ExecError(f"Access to private/dunder attribute '{a}' is not allowed", ln, kind="SAFETY"))
            elif a in FORBIDDEN_ATTRS or a.startswith(IO_ATTR_PREFIXES):
                errors.append(ExecError(f"Method '{a}' is not allowed (file/network IO or code loading)", ln, kind="SAFETY"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            # str.format can walk attributes ("{0.__class__}"); only literal templates without attribute/index fields
            recv = node.func.value
            if not (isinstance(recv, ast.Constant) and isinstance(recv.value, str)):
                errors.append(ExecError("str.format() is only allowed on a literal template; use an f-string", ln, kind="SAFETY"))
            else:
                import string as _string
                try:
                    fields = [f for _, f, _, _ in _string.Formatter().parse(recv.value) if f]
                except ValueError:
                    fields = ["?"]
                if any("." in f or "[" in f for f in fields):
                    errors.append(ExecError("Attribute or index access inside format fields is not allowed", ln, kind="SAFETY"))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            errors.append(ExecError("global/nonlocal statements are not allowed", ln, kind="SAFETY"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            errors.append(ExecError("String constants containing '__' are not allowed", ln, kind="SAFETY"))
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            errors.append(ExecError("async/generator constructs are not allowed", ln, kind="SAFETY"))
    seen, uniq = set(), []
    for e in errors:
        if (e.message, e.line) not in seen:
            seen.add((e.message, e.line))
            uniq.append(e)
    errors = sorted(uniq, key=lambda e: e.line or 0)[:25]
    if ".apply(" in code:
        warnings.append("Row-wise .apply() can be slow on large datasets; prefer vectorized operations.")
    return errors, warnings


IS_WINDOWS = os.name == "nt"


def _limits(mem_mb: int, cpu_s: int):
    if IS_WINDOWS:  # no rlimits/setsid on Windows: only the wall-clock timeout + kill apply (see README)
        return None

    def apply():  # runs in the child before exec
        import resource
        os.setsid()
        for res, val in ((resource.RLIMIT_AS, mem_mb * 1024 * 1024), (resource.RLIMIT_CPU, cpu_s),
                         (resource.RLIMIT_FSIZE, 2 * 1024 ** 3), (resource.RLIMIT_NOFILE, 256), (resource.RLIMIT_CORE, 0)):
            try:  # some platforms (e.g. macOS for RLIMIT_AS) reject individual limits; apply the rest
                resource.setrlimit(res, (val, val + 1 if res == resource.RLIMIT_CPU else val))
            except (ValueError, OSError):
                pass
    return apply


def execute_python(code: str, inputs: dict[str, Path], output_path: Path, *, limit: int | None = None,
                   timeout_s: int | None = None) -> tuple[list[ExecError], list[str], int, list[dict], list[str]]:
    """Returns (errors, logs, row_count, schema, warnings)."""
    s = get_settings()
    timeout_s = timeout_s or s.python_timeout_s
    errors, warnings = validate_python(code)
    if errors:
        return errors, [], 0, [], warnings
    with tempfile.TemporaryDirectory(prefix="qa_py_") as td:
        job = Path(td) / "job.json"
        result = Path(td) / "result.json"
        tmp_out = Path(td) / "out.parquet"
        job.write_text(json.dumps({"code": code, "inputs": {k: str(v) for k, v in inputs.items()},
                                   "output": str(tmp_out), "result": str(result), "limit": limit}))
        env = {"PATH": os.defpath, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "HOME": td, "TMPDIR": td}
        if IS_WINDOWS:  # Python on Windows needs SYSTEMROOT to start; TEMP/TMP point at the job dir
            env.update({"SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"), "TEMP": td, "TMP": td})
        t0 = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "-I", "-S", str(WORKER), str(job)], cwd=td, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                preexec_fn=_limits(s.python_memory_mb, timeout_s + 1))
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            if IS_WINDOWS:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return [ExecError(f"Python transformation exceeded the {timeout_s}s execution limit and was killed", kind="TIMEOUT")], [], 0, [], warnings
        dur = int((time.perf_counter() - t0) * 1000)
        logs = [l for l in out.decode("utf-8", "replace").splitlines()][-200:]
        if not result.exists():
            tail = err.decode("utf-8", "replace")[-1500:]
            msg = "Python worker terminated abnormally"
            killed = {-signal.SIGKILL, -getattr(signal, "SIGXCPU", 24)} if not IS_WINDOWS else set()
            if proc.returncode in killed or "MemoryError" in tail:
                msg += " (resource limit exceeded: memory or CPU)"
            return [ExecError(f"{msg}. {tail.strip().splitlines()[-1] if tail.strip() else ''}", kind="RUNTIME")], logs, 0, [], warnings
        res = json.loads(result.read_text())
        logs = res.get("logs", logs) + [f"Python worker finished in {dur} ms"]
        if res.get("error"):
            e = res["error"]
            return [ExecError(e["message"], e.get("line"), None, e.get("kind", "RUNTIME"))], logs, 0, [], warnings + res.get("warnings", [])
        shutil.move(str(tmp_out), str(output_path))  # works across drives (os.replace does not)
        return [], logs, res["row_count"], res["schema"], warnings + res.get("warnings", [])
