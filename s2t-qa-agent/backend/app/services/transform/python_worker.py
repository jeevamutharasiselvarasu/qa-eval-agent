"""Isolated worker for user/AI Python transformations. Run as: python -I -S python_worker.py job.json

Never imported by the API process. Writes a JSON result file; user output goes to the job's parquet path.
"""
import builtins
import json
import sys
import traceback

# site-packages are needed for pandas/pyarrow even with -S
import sysconfig
for _p in (sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]):
    if _p not in sys.path:
        sys.path.append(_p)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

ALLOWED_IMPORTS = {"pandas", "numpy", "math", "datetime", "decimal", "re", "statistics", "collections",
                   "itertools", "functools"}
SAFE_BUILTINS = ["abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "frozenset", "int", "isinstance",
                 "len", "list", "map", "max", "min", "print", "range", "repr", "reversed", "round", "set", "slice",
                 "sorted", "str", "sum", "tuple", "zip", "Exception", "ValueError", "KeyError", "TypeError",
                 "ZeroDivisionError", "IndexError", "None", "True", "False", "divmod", "pow", "hash", "format",
                 "issubclass", "type", "callable", "chr", "ord", "hex", "bin", "oct", "NotImplementedError",
                 "ArithmeticError", "AssertionError", "RuntimeError", "StopIteration", "iter", "next"]


def main(job_path: str) -> None:
    job = json.loads(open(job_path).read())
    result_path = job["result"]
    logs: list[str] = []
    warnings: list[str] = []

    def write_result(obj):
        with open(result_path, "w") as f:
            json.dump(obj, f, default=str)

    # Load inputs BEFORE locking down.
    sources = {}
    for alias, path in job["inputs"].items():
        sources[alias] = pq.read_table(path).to_pandas()
    import os
    norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731 - case/separator-insensitive on Windows
    lib_roots = tuple(norm(p).rstrip(os.sep) + os.sep for p in sys.path if p and os.path.isabs(p))
    phase = ["user"]

    def hook(event, args):
        if phase[0] != "user":
            return
        if event == "open":
            path, mode = str(args[0]), str(args[1] or "r")
            if any(c in mode for c in "wax+") or not norm(path).startswith(lib_roots):
                raise PermissionError(f"File access is not permitted in transformations: {path}")
        elif event.startswith(("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.fork", "os.posix_spawn",
                               "os.kill", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "shutil.", "ctypes.",
                               "os.chmod", "os.chown", "os.link", "os.symlink", "os.truncate", "urllib.", "http.",
                               "ftplib.", "smtplib.", "webbrowser.", "pty.", "sys._getframe", "code.", "compile",
                               "exec", "pickle.")):
            if event in ("exec", "compile"):
                return  # library-internal compile during lazy imports; user code cannot reach exec/compile (AST policy)
            raise PermissionError(f"Operation '{event}' is not permitted in transformations")

    sys.addaudithook(hook)

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name.split(".")[0] not in ALLOWED_IMPORTS:
            raise ImportError(f"Import of '{name}' is not allowed")
        return real_import(name, globals, locals, fromlist, level)

    safe = {k: getattr(builtins, k) for k in SAFE_BUILTINS if hasattr(builtins, k)}
    safe["__import__"] = guarded_import

    def user_print(*a, **k):
        if len(logs) < 500:
            logs.append(" ".join(str(x) for x in a)[:2000])

    safe["print"] = user_print
    ns = {"__builtins__": safe, "__name__": "transformation", "pd": pd, "np": np}
    try:
        exec(compile(job["code"], "<transformation>", "exec"), ns)  # noqa: S102 - code already passed AST policy
        out = ns["transform"]({k: v.copy() for k, v in sources.items()})
    except BaseException as e:  # noqa: BLE001
        phase[0] = "post"
        tb = traceback.extract_tb(e.__traceback__)
        user_frames = [f for f in tb if f.filename == "<transformation>"]
        line = user_frames[-1].lineno if user_frames else None
        kind = "SAFETY" if isinstance(e, (PermissionError, ImportError)) else "RUNTIME"
        write_result({"error": {"message": f"{type(e).__name__}: {e}"[:2000], "line": line, "kind": kind}, "logs": logs})
        return
    phase[0] = "post"
    if isinstance(out, pd.Series):
        out = out.to_frame()
    if not isinstance(out, pd.DataFrame):
        write_result({"error": {"message": f"transform() must return a pandas DataFrame, got {type(out).__name__}",
                                "kind": "CONTRACT"}, "logs": logs})
        return
    cols = [str(c) for c in out.columns]
    if len(set(c.lower() for c in cols)) != len(cols):
        write_result({"error": {"message": "Output has duplicate column names", "kind": "CONTRACT"}, "logs": logs})
        return
    if isinstance(out.index, pd.MultiIndex) or out.index.name is not None:
        out = out.reset_index()
        warnings.append("Output index was reset into columns.")
    out.columns = [str(c) for c in out.columns]
    for c in out.columns:
        if out[c].dtype == object:
            try:
                pa.array(out[c])
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                out[c] = out[c].map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
                warnings.append(f"Column {c} had mixed types and was converted to string.")
    if job.get("limit"):
        out = out.head(int(job["limit"]))
    table = pa.Table.from_pandas(out, preserve_index=False)
    pq.write_table(table, job["output"])
    write_result({"row_count": table.num_rows, "schema": [{"name": f.name, "type": str(f.type)} for f in table.schema],
                  "logs": logs, "warnings": warnings})


if __name__ == "__main__":
    main(sys.argv[1])
