"""Read source and target data from Azure Blob Storage / ADLS Gen2.

The app signs in with the machine identity (azure-identity DefaultAzureCredential: environment variables for a service
principal, workload or managed identity, or the Azure CLI login). No storage keys or SAS tokens are entered or stored.

Accepted paths (a file, or a folder of part files of one format, e.g. Spark / Databricks output):
  https://<account>.blob.core.windows.net/<container>/<path>
  https://<account>.dfs.core.windows.net/<filesystem>/<path>
  abfss://<container>@<account>.dfs.core.windows.net/<path>      (also abfs://, wasbs://, wasb://)
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import duckdb

from ..config import get_settings

FORMATS = {".csv", ".txt", ".tsv", ".json", ".ndjson", ".parquet", ".xlsx"}
COMBINABLE = {".csv", ".txt", ".tsv", ".json", ".ndjson", ".parquet"}
_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER = re.compile(r"^(?:\$root|[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62})$")
_HOST = re.compile(r"^(?P<account>[a-z0-9]+)\.(?P<svc>blob|dfs)\.core\.windows\.net$")


class BlobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BlobRef:
    account: str
    container: str
    path: str  # blob name or folder prefix, no leading slash

    @property
    def uri(self) -> str:
        return f"https://{self.account}.blob.core.windows.net/{self.container}/{self.path}".rstrip("/")

    @property
    def name(self) -> str:
        return PurePosixPath(self.path.rstrip("/")).name or self.container


def parse_path(raw: str) -> BlobRef:
    """Validate a storage path and split it into account, container and blob path."""
    text = (raw or "").strip()
    if not text:
        raise BlobError("Enter a storage path")
    if len(text) > 2000:
        raise BlobError("The storage path is too long")
    u = urlparse(text)
    if u.query or u.fragment:
        raise BlobError(
            "Remove the '?…' part (SAS token) from the path: the app signs in with the machine identity and never stores tokens"
        )
    scheme, host = u.scheme.lower(), (u.hostname or "").lower()
    if u.port or (scheme == "https" and (u.username is not None or u.password is not None)) or ":" in u.netloc.split("@")[-1]:
        raise BlobError("The path must not contain a port, user name or password")
    m = _HOST.match(host)
    if scheme == "https":
        if not m:
            raise BlobError("Use an Azure storage address: https://<account>.blob.core.windows.net/<container>/<path>")
        parts = unquote(u.path).lstrip("/").split("/", 1)
        container, path = parts[0], parts[1] if len(parts) > 1 else ""
    elif scheme in ("abfss", "abfs", "wasbs", "wasb"):
        if scheme in ("abfs", "wasb"):
            raise BlobError(f"{scheme}:// is unencrypted; use {scheme}s://")
        container, _, host = u.netloc.partition("@")
        m = _HOST.match(host.lower())
        if not container or not m:
            svc = "dfs" if scheme.startswith("abfs") else "blob"
            raise BlobError(f"Use {scheme}://<container>@<account>.{svc}.core.windows.net/<path>")
        path = unquote(u.path).lstrip("/")
    else:
        raise BlobError("Use an https://, abfss:// or wasbs:// Azure storage path")
    account = m.group("account")
    if not _ACCOUNT.match(account):
        raise BlobError(f"'{account}' is not a valid storage account name")
    if not _CONTAINER.match(container):
        raise BlobError(f"'{container}' is not a valid container name")
    if any(seg in ("..", ".") for seg in path.split("/")):
        raise BlobError("The path must not contain '.' or '..' segments")
    allowed = get_settings().blob_allowed_accounts
    if allowed and account not in allowed:
        raise BlobError(f"Storage account '{account}' is not in the allowed list (QA_BLOB_ALLOWED_ACCOUNTS)", 403)
    return BlobRef(account, container, path)


# ------------------------------------------------------------------ Azure access (replaced in tests)
@lru_cache(maxsize=1)
def _credential():
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as e:  # pragma: no cover - dependency is in requirements.txt
        raise BlobError("Azure support is not installed: run pip install -r requirements.txt", 500) from e
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def container_client(ref: BlobRef):
    from azure.storage.blob import ContainerClient

    return ContainerClient(f"https://{ref.account}.blob.core.windows.net", ref.container, credential=_credential())


def _azure_errors(ref: BlobRef, e: Exception) -> BlobError:
    name = type(e).__name__
    code = getattr(e, "status_code", None)
    if name in ("ClientAuthenticationError", "CredentialUnavailableError"):
        return BlobError(
            "Could not sign in to Azure with this machine's identity. Run 'az login' on the server, or set a managed identity "
            "or the AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET variables.",
            401,
        )
    if code == 403 or name == "AuthorizationFailure":
        return BlobError(
            f"This machine's identity has no read access to container '{ref.container}' in '{ref.account}'. "
            "It needs the 'Storage Blob Data Reader' role.",
            403,
        )
    if code == 404 or name == "ResourceNotFoundError":
        return BlobError(f"Container '{ref.container}' was not found in storage account '{ref.account}'", 404)
    if name == "ServiceRequestError":
        return BlobError(f"Could not reach storage account '{ref.account}' from the server", 502)
    return BlobError(f"Azure storage error: {e}", 502)


def _ext(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _is_data_part(name: str) -> bool:
    base = PurePosixPath(name).name
    return not base.startswith(("_", ".")) and _ext(base) in FORMATS


def list_files(ref: BlobRef) -> dict:
    """What the path points to: one file, or the data files of a folder (Spark markers like _SUCCESS are ignored)."""
    s = get_settings()
    try:
        cc = container_client(ref)
        exact = []
        if ref.path and not ref.path.endswith("/"):
            exact = [b for b in cc.list_blobs(name_starts_with=ref.path) if b.name == ref.path]
        if exact:
            b = exact[0]
            if _ext(b.name) not in FORMATS:
                raise BlobError(f"'{ref.name}' is not a supported file (CSV, TXT/TSV, XLSX, JSON, Parquet)")
            files = [{"name": b.name, "size": b.size}]
            kind = "file"
        else:
            prefix = ref.path.rstrip("/") + "/" if ref.path else ""
            files = []
            for b in cc.list_blobs(name_starts_with=prefix):
                if _is_data_part(b.name) and (b.size or 0) > 0:
                    files.append({"name": b.name, "size": b.size})
                    if len(files) > s.blob_max_files:
                        raise BlobError(f"The folder has more than {s.blob_max_files} files; point to a smaller folder or one file")
            kind = "folder"
    except BlobError:
        raise
    except Exception as e:  # noqa: BLE001 - Azure SDK errors are mapped to clear messages
        raise _azure_errors(ref, e) from e
    if not files:
        raise BlobError(f"Nothing found at {ref.uri} (no data files)", 404)
    exts = sorted({_ext(f["name"]) for f in files})
    if len(exts) > 1:
        raise BlobError(f"The folder mixes file types ({', '.join(exts)}); point to one file or a folder of one type")
    if kind == "folder" and exts[0] not in COMBINABLE:
        raise BlobError("A folder of Excel files cannot be combined; point to one .xlsx file")
    total = sum(f["size"] or 0 for f in files)
    if total > s.max_upload_bytes:
        raise BlobError(f"The data is {total:,} bytes, above the configured limit of {s.max_upload_bytes:,} (QA_MAX_UPLOAD_BYTES)", 413)
    return {"uri": ref.uri, "kind": kind, "format": exts[0].lstrip("."), "files": files, "total_bytes": total}


def check(raw: str) -> dict:
    ref = parse_path(raw)
    out = list_files(ref)
    return {**out, "account": ref.account, "container": ref.container, "file_count": len(out["files"]), "files": out["files"][:20]}


def _download(ref: BlobRef, name: str, dest: Path) -> None:
    try:
        stream = container_client(ref).get_blob_client(name).download_blob()
        with open(dest, "wb") as fh:
            stream.readinto(fh)
    except Exception as e:  # noqa: BLE001
        raise _azure_errors(ref, e) from e


def fetch(raw: str, work_dir: Path, opts: dict | None = None) -> tuple[Path, str, BlobRef, dict]:
    """Download the data at a storage path into work_dir. A folder of part files is combined into one Parquet file.

    Returns (local file, file name to show, parsed reference, listing)."""
    ref = parse_path(raw)
    listing = list_files(ref)
    tmp = Path(tempfile.mkdtemp(dir=work_dir, prefix=".blob_"))
    try:
        if listing["kind"] == "file":
            local = tmp / "data"
            _download(ref, listing["files"][0]["name"], local)
            out = Path(tempfile.NamedTemporaryFile(delete=False, dir=work_dir).name)
            shutil.move(str(local), out)
            return out, ref.name, ref, listing
        parts = []
        for i, f in enumerate(listing["files"]):
            p = tmp / f"part_{i:05d}{_ext(f['name'])}"
            _download(ref, f["name"], p)
            parts.append(str(p))
        out = Path(tempfile.NamedTemporaryFile(delete=False, dir=work_dir, suffix=".parquet").name)
        _combine(parts, listing["format"], out, opts or {})
        return out, f"{ref.name}.parquet", ref, listing
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _combine(parts: list[str], fmt: str, out: Path, opts: dict) -> None:
    files = "[" + ", ".join("'" + p.replace("'", "''") + "'" for p in parts) + "]"
    if fmt == "parquet":
        src = f"read_parquet({files}, union_by_name = true)"
    elif fmt in ("json", "ndjson"):
        src = f"read_json_auto({files}, union_by_name = true)"
    else:
        delim = opts.get("delimiter") or ("\t" if fmt == "tsv" else None)
        d = f", delim = '{delim}'" if delim and len(delim) == 1 and delim != "'" else ""
        src = f"read_csv({files}, auto_detect = true, sample_size = -1, header = true, union_by_name = true{d})"
    con = duckdb.connect()
    try:
        con.execute("SET threads = 1")  # keeps row order, so the same files always give the same fingerprint
        con.execute(f"COPY (SELECT * FROM {src}) TO '{str(out).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)")
    except duckdb.Error as e:
        raise BlobError(f"The files in the folder could not be combined: {e}", 422) from e
    finally:
        con.close()
