#!/usr/bin/env python3
"""Smoke-test a local Document Manager install with real sample documents."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_USERNAME = "codex-smoke-admin"
DEFAULT_EMAIL = "codex-smoke@example.local"
SMOKE_CREDENTIALS_FILE = ".local-smoke-credentials.json"

TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".text"}
STOP_WORDS = {
    "about",
    "after",
    "again",
    "because",
    "before",
    "between",
    "could",
    "document",
    "files",
    "first",
    "from",
    "have",
    "their",
    "there",
    "these",
    "thing",
    "those",
    "through",
    "where",
    "which",
    "while",
    "would",
}


@dataclass
class HttpResult:
    status: int
    body: Any
    text: str
    headers: dict[str, str]


@dataclass
class Sample:
    source: Path
    target_name: str
    search_query: str


@dataclass
class SmokeCredentials:
    username: str
    email: str
    password: str
    path: Path
    generated: bool


class SmokeFailure(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.csrf_token: str | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        csrf: bool = False,
        timeout: int = 20,
    ) -> HttpResult:
        url = urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/"))
        data = None
        headers = {"User-Agent": "documentmanager-local-smoke/1.0"}

        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if csrf:
            token = self.get_csrf_token()
            headers["X-CSRF-Token"] = token

        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result = HttpResult(
                    status=response.status,
                    body=self._parse_body(raw),
                    text=raw,
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            result = HttpResult(
                status=error.code,
                body=self._parse_body(raw),
                text=raw,
                headers=dict(error.headers.items()),
            )

        header_token = get_header(result.headers, "X-CSRF-Token")
        if header_token:
            self.csrf_token = header_token
        else:
            cookie_token = self._csrf_from_cookie()
            if cookie_token:
                self.csrf_token = cookie_token

        return result

    def get_csrf_token(self) -> str:
        cookie_token = self._csrf_from_cookie()
        if cookie_token:
            self.csrf_token = cookie_token
            return cookie_token

        if self.csrf_token:
            return self.csrf_token

        result = self.request("GET", "/api/csrf-token")
        if isinstance(result.body, dict) and result.body.get("csrf_token"):
            self.csrf_token = str(result.body["csrf_token"])

        if not self.csrf_token:
            self.csrf_token = self._csrf_from_cookie()

        if not self.csrf_token:
            raise SmokeFailure("Could not obtain CSRF token from the running app")

        return self.csrf_token

    def _csrf_from_cookie(self) -> str | None:
        for cookie in self.cookie_jar:
            if cookie.name == "csrf_token" and cookie.value:
                return cookie.value.split(".", 1)[0]
        return None

    @staticmethod
    def _parse_body(raw: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


def get_header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def wait_for_app(client: ApiClient, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"

    while time.monotonic() < deadline:
        try:
            result = client.request("GET", "/api/health/simple", timeout=5)
            if result.status == 200 and isinstance(result.body, dict):
                if result.body.get("status") == "ok":
                    return
            last_error = result.text or f"HTTP {result.status}"
        except Exception as error:  # noqa: BLE001 - command-line smoke needs context
            last_error = str(error)
        time.sleep(2)

    raise SmokeFailure(f"App did not become healthy in time: {last_error}")


def ensure_admin_session(client: ApiClient, args: argparse.Namespace) -> None:
    setup = retry_request(client, "GET", "/api/auth/setup/check", args.timeout)

    if setup.status != 200 or not isinstance(setup.body, dict):
        raise SmokeFailure(f"Setup check failed: HTTP {setup.status} {setup.text}")

    if not setup.body.get("setup_complete"):
        created = client.request(
            "POST",
            "/api/auth/setup/initial-user",
            {
                "username": args.admin_username,
                "email": args.admin_email,
                "full_name": "Document Manager Local Smoke",
                "password": args.admin_password,
                "is_admin": True,
            },
        )
        if created.status not in {200, 201}:
            raise SmokeFailure(f"Initial admin creation failed: HTTP {created.status} {created.text}")

    login = login_with_retry(client, args)
    if login.status != 200:
        raise SmokeFailure(
            "Login failed for the smoke admin. If this data directory already has "
            "different users, rerun with a fresh data dir or set "
            "DM_SMOKE_USERNAME/DM_SMOKE_PASSWORD for an existing admin. "
            f"HTTP {login.status} {login.text}"
        )

    me = client.request("GET", "/api/auth/me")
    if me.status != 200:
        raise SmokeFailure(f"Authenticated /api/auth/me check failed: HTTP {me.status} {me.text}")

    client.get_csrf_token()


def resolve_smoke_credentials(args: argparse.Namespace, data_dir: Path) -> SmokeCredentials:
    credential_path = data_dir / SMOKE_CREDENTIALS_FILE

    if args.admin_password:
        return SmokeCredentials(
            username=args.admin_username,
            email=args.admin_email,
            password=args.admin_password,
            path=credential_path,
            generated=False,
        )

    if credential_path.exists():
        stored = read_smoke_credentials(credential_path)
        stored_password = str(stored.get("password") or "")
        if stored_password:
            return SmokeCredentials(
                username=str(stored.get("username") or args.admin_username),
                email=str(stored.get("email") or args.admin_email),
                password=stored_password,
                path=credential_path,
                generated=False,
            )

    return SmokeCredentials(
        username=args.admin_username,
        email=args.admin_email,
        password=secrets.token_urlsafe(24),
        path=credential_path,
        generated=True,
    )


def read_smoke_credentials(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as credential_file:
            loaded = json.load(credential_file)
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"Smoke credential file is invalid JSON: {path}") from error

    if not isinstance(loaded, dict):
        raise SmokeFailure(f"Smoke credential file must contain a JSON object: {path}")
    return loaded


def write_smoke_credentials(credentials: SmokeCredentials) -> None:
    credentials.path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "username": credentials.username,
        "email": credentials.email,
        "password": credentials.password,
    }

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(credentials.path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as credential_file:
        json.dump(payload, credential_file, indent=2)
        credential_file.write("\n")


def login_with_retry(client: ApiClient, args: argparse.Namespace) -> HttpResult:
    deadline = time.monotonic() + args.timeout

    while True:
        login = client.request(
            "POST",
            "/api/auth/login",
            {"username": args.admin_username, "password": args.admin_password},
        )
        if login.status != 429 or time.monotonic() >= deadline:
            return login

        retry_after = 5
        if isinstance(login.body, dict):
            try:
                retry_after = int(login.body.get("retry_after") or retry_after)
            except (TypeError, ValueError):
                retry_after = 5
        sleep_for = min(max(retry_after, 1), max(int(deadline - time.monotonic()), 1))
        time.sleep(sleep_for)


def retry_request(client: ApiClient, method: str, path: str, timeout_seconds: int) -> HttpResult:
    deadline = time.monotonic() + timeout_seconds
    last = HttpResult(status=0, body=None, text="not attempted", headers={})

    while time.monotonic() < deadline:
        last = client.request(method, path, timeout=5)
        if last.status < 500:
            return last
        time.sleep(2)

    return last


def select_samples(sample_dir: Path, sample_count: int) -> list[Sample]:
    if not sample_dir.exists():
        raise SmokeFailure(f"Sample directory does not exist: {sample_dir}")
    if not sample_dir.is_dir():
        raise SmokeFailure(f"Sample path is not a directory: {sample_dir}")

    markdowns: list[Path] = []
    text_files: list[Path] = []

    for path in sorted(sample_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if path.stat().st_size < 32 or path.stat().st_size > 2_000_000:
            continue
        if path.suffix.lower() in {".md", ".markdown"}:
            markdowns.append(path)
        else:
            text_files.append(path)

    if not markdowns:
        raise SmokeFailure(f"No readable Markdown sample found under {sample_dir}")
    if not text_files:
        raise SmokeFailure(f"No readable text sample found under {sample_dir}")

    chosen = [markdowns[0], text_files[0]]
    remaining = [p for p in [*markdowns[1:], *text_files[1:]] if p not in chosen]
    chosen.extend(remaining[: max(0, sample_count - len(chosen))])

    samples = []
    for source in chosen[:sample_count]:
        query = choose_search_query(read_text(source))
        rel = safe_relative(source, sample_dir)
        digest = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:8]
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-")[:60]
        if not safe_stem:
            safe_stem = "sample"
        target_name = f"smoke_{safe_stem}_{digest}{source.suffix.lower()}"
        samples.append(Sample(source=source, target_name=target_name, search_query=query))

    return samples


def read_text(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def choose_search_query(text: str) -> str:
    for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]{5,}", text):
        normalized = word.strip("'").lower()
        if normalized and normalized not in STOP_WORDS and not normalized.startswith("http"):
            return normalized
    raise SmokeFailure("Could not derive a search query from the selected samples")


def safe_relative(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return Path(path.name)


def stage_samples(samples: list[Sample], data_dir: Path) -> list[Path]:
    staging_dir = data_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)

    staged_paths = []
    for sample in samples:
        target = staging_dir / sample.target_name
        shutil.copyfile(sample.source, target)
        staged_paths.append(target)

    return staged_paths


def trigger_processing(client: ApiClient) -> None:
    result = client.request("POST", "/api/documents/process-staging", {}, csrf=True)
    if result.status not in {200, 202}:
        raise SmokeFailure(f"Could not trigger staging processing: HTTP {result.status} {result.text}")


def wait_for_processed_documents(
    db_path: Path,
    expected_filenames: list[str],
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_rows: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        if db_path.exists():
            last_rows = fetch_document_rows(db_path, expected_filenames)
            completed = {
                row["original_filename"]
                for row in last_rows
                if row["ocr_status"] == "completed" and row["full_text_length"] > 0
            }
            if set(expected_filenames).issubset(completed):
                return last_rows
        time.sleep(2)

    statuses = ", ".join(
        f"{row['original_filename']}={row['ocr_status']}:{row['full_text_length']}"
        for row in last_rows
    )
    raise SmokeFailure(
        "Timed out waiting for processed sample documents. "
        f"Observed rows: {statuses or 'none'}"
    )


def fetch_document_rows(db_path: Path, expected_filenames: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in expected_filenames)
    query = f"""
        SELECT original_filename, filename, ocr_status, ai_status, vector_status,
               COALESCE(LENGTH(full_text), 0) AS full_text_length
        FROM documents
        WHERE original_filename IN ({placeholders})
        ORDER BY created_at DESC
    """

    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, expected_filenames)]
    finally:
        connection.close()


def verify_api_documents(client: ApiClient, expected_filenames: list[str]) -> list[dict[str, Any]]:
    result = client.request("GET", "/api/documents/?limit=100")
    if result.status != 200 or not isinstance(result.body, list):
        raise SmokeFailure(f"Document list API failed: HTTP {result.status} {result.text}")

    found_by_name = {
        doc.get("original_filename"): doc
        for doc in result.body
        if doc.get("original_filename") in expected_filenames
    }
    missing = [name for name in expected_filenames if name not in found_by_name]
    if missing:
        raise SmokeFailure(
            "Document list API did not return all smoke sample documents: "
            + ", ".join(missing)
        )
    return [found_by_name[name] for name in expected_filenames]


def verify_search(client: ApiClient, search_query: str) -> int:
    result = client.request(
        "POST",
        "/api/search/",
        {"query": search_query, "limit": 10, "use_semantic_search": False},
        csrf=True,
    )
    if result.status != 200 or not isinstance(result.body, dict):
        raise SmokeFailure(f"Search API failed: HTTP {result.status} {result.text}")

    total = int(result.body.get("total_count") or 0)
    if total < 1:
        raise SmokeFailure(f"Search API returned no results for query '{search_query}'")
    return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a local Document Manager install using sample documents."
    )
    parser.add_argument("--base-url", default=os.environ.get("DM_SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--data-dir", default=os.environ.get("DM_SMOKE_DATA_DIR", "data"))
    parser.add_argument(
        "--sample-dir",
        default=os.environ.get("DM_SMOKE_SAMPLE_DIR", "~/projects/comedy/docs"),
    )
    parser.add_argument("--sample-count", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--admin-username",
        default=os.environ.get("DM_SMOKE_USERNAME", DEFAULT_USERNAME),
    )
    parser.add_argument(
        "--admin-password",
        default=os.environ.get("DM_SMOKE_PASSWORD"),
    )
    parser.add_argument(
        "--admin-email",
        default=os.environ.get("DM_SMOKE_EMAIL", DEFAULT_EMAIL),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    sample_dir = Path(args.sample_dir).expanduser().resolve()
    db_path = data_dir / "documents.db"

    client = ApiClient(args.base_url)

    try:
        wait_for_app(client, args.timeout)
        credentials = resolve_smoke_credentials(args, data_dir)
        args.admin_username = credentials.username
        args.admin_email = credentials.email
        args.admin_password = credentials.password
        ensure_admin_session(client, args)
        if credentials.generated:
            write_smoke_credentials(credentials)
        samples = select_samples(sample_dir, max(2, args.sample_count))
        stage_samples(samples, data_dir)
        trigger_processing(client)
        rows = wait_for_processed_documents(
            db_path,
            [sample.target_name for sample in samples],
            args.timeout,
        )
        api_documents = verify_api_documents(client, [sample.target_name for sample in samples])
        search_query = samples[0].search_query
        search_total = verify_search(client, search_query)
    except SmokeFailure as error:
        print(f"Smoke test failed: {error}", file=sys.stderr)
        return 1

    print("Smoke test passed")
    print(f"base_url={args.base_url}")
    print(f"data_dir={data_dir}")
    print(f"sample_dir={sample_dir}")
    print("samples=" + ",".join(sample.target_name for sample in samples))
    print(f"processed_rows={len(rows)}")
    print(f"api_documents={len(api_documents)}")
    print(f"search_query={search_query}")
    print(f"search_results={search_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
