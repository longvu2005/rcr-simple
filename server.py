"""Small, dependency-free RCR annotation server. Run `python server.py --help`."""

from __future__ import annotations

import argparse
import csv
import errno
import getpass
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from core import (
    InputError,
    normalize_annotation,
    normalize_task,
    parse_json_tasks,
    validate_email,
)
from persistence import backup_database, export_record, write_snapshot

STATIC = Path(__file__).with_name("static")
COOKIE = "rcr_session"
SESSION_AGE = 7 * 24 * 3600
LOG = logging.getLogger("rcr")


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


class Unauthorized(Exception):
    pass


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def init_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            email TEXT, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('ADMIN','ANNOTATOR')),
            active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            sample_id TEXT PRIMARY KEY, case_type TEXT NOT NULL, split TEXT,
            query_image_id TEXT NOT NULL, target_image_id TEXT NOT NULL,
            target_image_ids_json TEXT NOT NULL, query_image_path TEXT NOT NULL,
            target_image_path TEXT NOT NULL, query_boxes_json TEXT NOT NULL,
            target_boxes_json TEXT NOT NULL, candidates_json TEXT NOT NULL,
            initial_subjects_json TEXT NOT NULL,
            assignee_id INTEGER REFERENCES users(id),
            status TEXT NOT NULL CHECK(status IN ('UNASSIGNED','ASSIGNED','IN_PROGRESS','SUBMITTED')),
            revision INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            started_at INTEGER, submitted_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS tasks_assignee ON tasks(assignee_id,status);
        CREATE INDEX IF NOT EXISTS tasks_case_split ON tasks(case_type,split);
        CREATE TABLE IF NOT EXISTS annotations (
            task_id TEXT PRIMARY KEY REFERENCES tasks(sample_id) ON DELETE CASCADE,
            author_id INTEGER REFERENCES users(id), annotator_email TEXT,
            data_json TEXT NOT NULL,
            submitted INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS annotation_history (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            author_id INTEGER, annotator_email TEXT, data_json TEXT NOT NULL,
            submitted INTEGER NOT NULL, archived_at INTEGER NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS annotations_author ON annotations(author_id,submitted);
        CREATE INDEX IF NOT EXISTS history_author ON annotation_history(author_id,submitted,task_id);
        """)
        # Additive migrations keep databases created by earlier releases usable.
        db.execute("BEGIN IMMEDIATE")
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT")
        annotation_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(annotations)")
        }
        if "annotator_email" not in annotation_columns:
            db.execute("ALTER TABLE annotations ADD COLUMN annotator_email TEXT")
        history_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(annotation_history)")
        }
        if "annotator_email" not in history_columns:
            db.execute("ALTER TABLE annotation_history ADD COLUMN annotator_email TEXT")
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        if "reopened" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN reopened INTEGER NOT NULL DEFAULT 0")
            # Older reopened rows retain final text until their first draft save.
            for row in db.execute("""
                SELECT t.sample_id,a.data_json FROM tasks t
                JOIN annotations a ON a.task_id=t.sample_id
                WHERE t.status='IN_PROGRESS' AND a.submitted=0
            """).fetchall():
                if "final_instruction" in json.loads(row["data_json"]):
                    db.execute("UPDATE tasks SET reopened=1 WHERE sample_id=?", (row["sample_id"],))
        # Keep completion counts stable across reopen/edit/reassign cycles without
        # adding duplicate annotation versions to annotation_history.
        db.execute("""CREATE TABLE IF NOT EXISTS task_completions (
            task_id TEXT NOT NULL REFERENCES tasks(sample_id) ON DELETE CASCADE,
            author_id INTEGER NOT NULL REFERENCES users(id),
            PRIMARY KEY(task_id,author_id)
        )""")
        db.execute("""INSERT OR IGNORE INTO task_completions(task_id,author_id)
            SELECT a.task_id,a.author_id FROM annotations a
            JOIN tasks t ON t.sample_id=a.task_id JOIN users u ON u.id=a.author_id
            WHERE a.submitted=1 OR t.reopened=1
            UNION
            SELECT h.task_id,h.author_id FROM annotation_history h
            JOIN tasks t ON t.sample_id=h.task_id JOIN users u ON u.id=h.author_id
            WHERE h.submitted=1
        """)
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique "
            "ON users(email COLLATE NOCASE) WHERE email IS NOT NULL"
        )
        db.commit()


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or not 4 <= len(password) <= 1024:
        raise InputError("password must have 4–1,024 characters")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
        actual = hash_password(password, bytes.fromhex(salt)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def parse_bulk_users(text: str) -> list[tuple[str, str | None, str]]:
    """Parse username,password or username,email,password CSV atomically."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 30_000:
        raise InputError("user list must be text under 30 KB")
    users: list[tuple[str, str | None, str]] = []
    seen: set[str] = set()
    try:
        for line, row in enumerate(
            csv.reader(io.StringIO(text, newline=""), strict=True), 1
        ):
            if not row:
                continue
            if len(row) == 2:
                username, password = row[0].strip(), row[1]
                email = None
            elif len(row) == 3:
                username, email_value, password = row[0].strip(), row[1], row[2]
                try:
                    email = validate_email(email_value, required=True)
                except InputError as exc:
                    raise InputError(f"line {line}: {exc}") from exc
            else:
                raise InputError(
                    f"line {line}: expected username,password or username,email,password"
                )
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
                raise InputError(f"line {line}: invalid username")
            if username in seen:
                raise InputError(f"line {line}: duplicate username {username}")
            if len(password) < 4:
                raise InputError(
                    f"line {line}: password must have at least 4 characters"
                )
            seen.add(username)
            users.append((username, email, password))
            if len(users) > 200:
                raise InputError("create at most 200 users per batch")
    except csv.Error as exc:
        raise InputError(f"invalid CSV: {exc}") from exc
    if not users:
        raise InputError("enter at least one username,password line")
    return users


def iso(ts: int | None) -> int | None:
    return ts  # UTC epoch seconds; clients format in the user's local timezone.


def validate_import(
    db: sqlite3.Connection, text: str
) -> tuple[list[dict], int, list[dict], int]:
    rows = parse_json_tasks(text)
    existing = {r[0] for r in db.execute("SELECT sample_id FROM tasks")}
    seen, valid, errors = set(), [], []
    invalid = 0
    duplicate = 0
    for line, raw in enumerate(rows, 1):
        try:
            item = normalize_task(raw)
            imported_email = validate_email(
                raw.get("annotator_email"), required=False
            )
            if imported_email is not None and item["imported_annotation"] is None:
                raise InputError(
                    "annotator_email is only valid when the row contains an imported annotation"
                )
            item["imported_annotator_email"] = imported_email
            sid = item["sample_id"]
            if sid in seen:
                raise InputError(f"duplicate sample_id in import: {sid}")
            seen.add(sid)
            if sid in existing:
                duplicate += 1
                continue
            if item["imported_annotation"] is not None:
                item["imported_annotation"] = normalize_annotation(
                    item["imported_annotation"], item["candidate_identity_ids"], True
                )
            valid.append(item)
        except (InputError, ValueError) as exc:
            invalid += 1
            if len(errors) < 20:
                errors.append({"line": line, "error": str(exc)})
    return valid, duplicate, errors, invalid


def row_task(row: sqlite3.Row, include_images: bool = False) -> dict:
    result = {
        "sample_id": row["sample_id"],
        "case_type": row["case_type"],
        "split": row["split"],
        "status": row["status"],
        "assignee_id": row["assignee_id"],
        "revision": row["revision"],
        "updated_at": iso(row["updated_at"]),
        "submitted_at": iso(row["submitted_at"]),
        "reopened": bool(row["reopened"]),
    }
    if include_images:
        sid = quote(row["sample_id"], safe="")
        result.update(
            query={
                "image_id": row["query_image_id"],
                "image_url": f"/api/image/{sid}/query",
                "boxes": json.loads(row["query_boxes_json"]),
            },
            target={
                "image_id": row["target_image_id"],
                "image_url": f"/api/image/{sid}/target",
                "boxes": json.loads(row["target_boxes_json"]),
            },
            target_image_ids=json.loads(row["target_image_ids_json"]),
            candidate_identity_ids=json.loads(row["candidates_json"]),
            initial_subjects=json.loads(row["initial_subjects_json"]),
        )
    return result


def local_image(image_root: Path | None, relative: str) -> Path:
    if image_root is None:
        raise InputError("RCR_IMAGE_ROOT is not configured")
    # Path was checked at import. Resolve again so symlinks cannot escape the root.
    root = image_root.resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise Forbidden("unsafe image path")
    return path


def gemini_suggestion(
    model: str,
    key: str,
    case: str,
    subjects: list[dict],
    descriptions: list[str],
    previous: str,
    note: str,
) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", model):
        raise InputError("invalid RCR_GEMINI_MODEL")
    case_rule = {
        "INDIVIDUAL": "Edit the TARGET draft so it describes a change/state of Subject 1 only.",
        "GROUP": "Edit the TARGET draft for Subject 1 as a group; keep its members together and do not rewrite the group as one person.",
        "DUAL": "Edit the TARGET draft as independent changes for Subject 1 and Subject 2; do not introduce a relation that the human did not write.",
        "RELATIONAL": "Edit the TARGET draft as an explicit directed relationship, preserving exactly who does what to whom between Subject 1 and Subject 2.",
    }[case]
    prompt = (
        "You are a LANGUAGE EDITOR for a human-authored RCR annotation, not an annotation generator "
        "and not a visual verifier. No images are provided. "
        "The human drafts below are the source of truth for annotation content and semantics. "
        "They may be terse notes, fragments, Vietnamese, English, or a mixture of both. "
        "Translate Vietnamese content to ENGLISH, fix grammar and phrasing, and normalize the text to "
        "the required RCR format. Every returned SELECT and TARGET string MUST be in English. "
        "Preserve the human's meaning, Subject roles, relation direction, actions, objects, attributes, "
        "and level of detail. Do not infer, invent, enrich, or add any visual fact that is absent from "
        "the human draft. Do not silently correct a possible visual mistake because you cannot see the images. "
        "If an English phrase is already correct and well formatted, keep it as unchanged as possible. "
        "The case type and Subject assignments are fixed; never reinterpret or change them. "
        "Return only a JSON object with select_texts (array of English strings) and "
        "target_condition (English string). "
        "For each SELECT text, turn the corresponding human QUERY draft into a concise, fluent English "
        "noun phrase; do not include 'Identify Subject' or labels such as 'Subject 1'/'Subject 2' in SELECT. "
        "For TARGET, turn the human TARGET draft into fluent English, explicitly naming Subject 1 "
        "(and Subject 2 for two-subject cases). Do not include 'then retrieve target images where'. "
        "Keep one SELECT text per Subject in the original order. Do not output placeholders, commentary, "
        "explanations, or extra keys. "
        f"Case: {case}. {case_rule}\n"
        f"Human SELECT drafts to edit/translate: {json.dumps(descriptions, ensure_ascii=False)}\n"
        f"Human TARGET draft to edit/translate: {previous}\n"
        f"Optional human clarification (context only; do not use it to replace or expand the drafts): {note}"
    )
    body = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 4096,
                "thinkingConfig": ({"thinkingBudget": 1024}
                                   if model.startswith("gemini-2.5-")
                                   else {"thinkingLevel": "low"}),
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "select_texts": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "target_condition": {"type": "STRING"},
                    },
                    "required": ["select_texts", "target_condition"],
                },
            },
        },
        ensure_ascii=False,
    ).encode()
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise InputError(
            f"LLM returned HTTP {exc.code}; verify key/model/quota"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise InputError(
            "LLM is unavailable; you can still edit the annotation manually"
        ) from exc
    except (ValueError, UnicodeError) as exc:
        raise InputError("Gemini returned invalid JSON; retry or edit manually") from exc
    if not isinstance(result, dict):
        raise InputError("Gemini returned an invalid response")
    if (
        isinstance(result.get("candidates"), list)
        and result["candidates"]
        and isinstance(result["candidates"][0], dict)
        and result["candidates"][0].get("finishReason") == "MAX_TOKENS"
    ):
        raise InputError("Gemini response was truncated; retry or edit manually")
    try:
        text = "".join(
            p["text"]
            for p in result["candidates"][0]["content"]["parts"]
            if "text" in p and not p.get("thought")
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise InputError("LLM returned no text suggestion") from exc
    try:
        answer = json.loads(text)
        if not isinstance(answer, dict):
            raise ValueError("expected an object")
        normalized = normalize_annotation(
            {
                "case_type": case,
                "subjects": subjects,
                "select_texts": answer["select_texts"],
                "target_condition": answer["target_condition"],
            },
            [identity for subject in subjects for identity in subject["identity_ids"]],
            True,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise InputError(
            "LLM output is invalid; retry or edit the annotation manually"
        ) from exc
    return {
        "select_texts": normalized["select_texts"],
        "target_condition": normalized["target_condition"],
        "final_instruction": normalized["final_instruction"],
    }


class Handler(BaseHTTPRequestHandler):
    db_path: Path
    image_root: Path | None
    gemini_key: str
    gemini_model: str
    cookie_secure: bool
    login_attempts: dict[str, list[int]] = {}
    login_lock = threading.Lock()
    llm_slots = threading.BoundedSemaphore(2)
    llm_users: set[int] = set()
    llm_lock = threading.Lock()

    def setup(self) -> None:
        self.request.settimeout(90)
        super().setup()

    def record_snapshot(self) -> str | None:
        try:
            write_snapshot(self.db_path)
        except (OSError, sqlite3.Error):
            LOG.exception("Final JSONL refresh failed; committed data remains in SQLite")
            return "Saved in SQLite, but the final JSONL could not be updated"
        return None

    def log_message(self, format: str, *args: object) -> None:
        # Do not log task IDs, URL queries, passwords or cookies.
        return

    def _headers(
        self, code: int, kind: str, length: int, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; script-src 'self'; "
            "style-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def send_json(
        self, value: object, code: int = 200, extra: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(data), extra)
        self.wfile.write(data)

    def send_file(
        self, data: bytes, kind: str, extra: dict[str, str] | None = None
    ) -> None:
        self._headers(200, kind, len(data), extra)
        self.wfile.write(data)

    def body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise InputError("invalid Content-Length") from exc
        if length <= 0 or length > 35_000_000:
            raise InputError("invalid or too large request body")
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise InputError("send application/json")
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeError) as exc:
            raise InputError("invalid request JSON") from exc
        if not isinstance(value, dict):
            raise InputError("request must be a JSON object")
        return value

    def user(self, db: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar[COOKIE].value if COOKIE in jar else ""
        except Exception:
            token = ""
        if not token:
            raise Unauthorized("please sign in")
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute(
            """
            SELECT s.token_hash, s.csrf, s.expires_at,
                   u.id, u.username, u.email, u.role, u.active
            FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=?
        """,
            (digest,),
        ).fetchone()
        if row is None or not row["active"] or row["expires_at"] <= int(time.time()):
            raise Unauthorized("session expired; please sign in again")
        return row, row

    def require(
        self, db: sqlite3.Connection, role: str | None = None, write: bool = False
    ) -> sqlite3.Row:
        user, session = self.user(db)
        if role and user["role"] != role:
            raise Forbidden("this page is not available to your role")
        if write and not hmac.compare_digest(
            self.headers.get("X-CSRF-Token", "").encode("utf-8"),
            session["csrf"].encode("utf-8"),
        ):
            raise Forbidden("CSRF token is missing or invalid")
        return user

    def same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            scheme = "https" if self.cookie_secure else "http"
            expected = f"{scheme}://{self.headers.get('Host', '')}"
            if origin != expected:
                raise Forbidden("cross-origin requests are not allowed")

    def _dispatch(self, method: str) -> None:
        db = None
        try:
            db = connect(self.db_path)
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            if method == "GET" and (path == "/" or path in ("/app.js", "/style.css")):
                name = "index.html" if path == "/" else path[1:]
                kind = {
                    "index.html": "text/html",
                    "app.js": "text/javascript",
                    "style.css": "text/css",
                }[name]
                self.send_file((STATIC / name).read_bytes(), kind + "; charset=utf-8")
                return
            if method == "POST":
                self.same_origin()
            if path == "/api/auth/login" and method == "POST":
                self.login(db, self.body())
                return
            if path == "/api/me" and method == "GET":
                user, session = self.user(db)
                self.send_json(
                    {
                        "user": {
                            "id": user["id"],
                            "username": user["username"],
                            "email": user["email"],
                            "role": user["role"],
                        },
                        "csrf": session["csrf"],
                        "llm_enabled": bool(self.gemini_key and self.gemini_model),
                    }
                )
                return
            if path == "/api/auth/logout" and method == "POST":
                user = self.require(db, write=True)
                db.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token_hash=?",
                    (user["id"], self.user(db)[1]["token_hash"]),
                )
                extra = {
                    "Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                }
                self.send_json({"ok": True}, extra=extra)
                return
            if path.startswith("/api/image/") and method == "GET":
                self.image(db, path)
                return
            if path.startswith("/api/admin/"):
                user = self.require(db, "ADMIN", method != "GET")
                self.admin(db, path, query, method, user)
                return
            if path.startswith("/api/work/"):
                user = self.require(db, "ANNOTATOR", method != "GET")
                self.work(db, path, method, user)
                return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Unauthorized as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except Forbidden as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Conflict as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except (InputError, ValueError, KeyError, sqlite3.IntegrityError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except FileNotFoundError:
            self.send_json(
                {"error": "image missing; check RCR_IMAGE_ROOT"}, HTTPStatus.NOT_FOUND
            )
        except PermissionError:
            self.send_json({"error": "Server cannot access its files; check filesystem permissions"},
                           HTTPStatus.SERVICE_UNAVAILABLE)
        except sqlite3.OperationalError as exc:
            LOG.error("SQLite operation failed: %s", exc)
            self.send_json(
                {"error": "Database unavailable or busy; keep your draft and retry"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # A disconnected browser must not roll back an already committed save.
            return
        except Exception:
            LOG.exception("Unexpected server error in %s", method)
            self.send_json(
                {"error": "unexpected server error"}, HTTPStatus.INTERNAL_SERVER_ERROR
            )
        finally:
            if db is not None:
                db.close()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def login(self, db: sqlite3.Connection, data: dict) -> None:
        ip = self.client_address[0]
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise InputError("username and password are required")
        if len(username) > 40 or len(password) > 1024:
            raise InputError("username or password is too long")
        attempt_key = ip + ":" + username
        now = int(time.time())
        with self.login_lock:
            for key in list(self.login_attempts):
                if not any(now - x < 60 for x in self.login_attempts[key]):
                    del self.login_attempts[key]
            recent = [x for x in self.login_attempts.get(attempt_key, []) if now - x < 60]
            if len(recent) >= 8:
                raise Forbidden("too many login attempts; wait one minute")
            self.login_attempts[attempt_key] = recent + [now]
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = (
            row and row["active"] and verify_password(password, row["password_hash"])
        )
        if not valid:
            raise Unauthorized("invalid username or password")
        with self.login_lock:
            self.login_attempts.pop(attempt_key, None)
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                row["id"],
                csrf,
                now + SESSION_AGE,
            ),
        )
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_AGE}"
        if self.cookie_secure:
            cookie += "; Secure"
        self.send_json(
            {
                "user": {
                    "id": row["id"],
                    "username": row["username"],
                    "email": row["email"],
                    "role": row["role"],
                },
                "csrf": csrf,
            },
            extra={"Set-Cookie": cookie},
        )

    def image(self, db: sqlite3.Connection, path: str) -> None:
        user = self.require(db)
        match = re.fullmatch(r"/api/image/([^/]+)/(query|target)", path)
        if not match:
            raise InputError("invalid image URL")
        sid, side = unquote(match[1]), match[2]
        row = db.execute("SELECT * FROM tasks WHERE sample_id=?", (sid,)).fetchone()
        if not row:
            raise InputError("unknown task")
        if user["role"] != "ADMIN" and row["assignee_id"] != user["id"]:
            raise Forbidden("image does not belong to your task")
        path = local_image(self.image_root, row[f"{side}_image_path"])
        self.send_file(
            path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/jpeg"
        )

    def admin(
        self,
        db: sqlite3.Connection,
        path: str,
        query: dict,
        method: str,
        user: sqlite3.Row,
    ) -> None:
        if path == "/api/admin/users" and method == "GET":
            rows = db.execute("""
                SELECT u.id,u.username,u.email,u.role,u.active,u.created_at,
                       COALESCE(t.assigned,0) AS assigned,
                       COALESCE(t.pending,0) AS pending,
                       COALESCE(t.in_progress,0) AS in_progress,
                       COALESCE(a.completed,0) AS completed,
                       COALESCE(h.ever_completed,0) AS ever_completed
                FROM users u LEFT JOIN (
                    SELECT assignee_id, COUNT(*) AS assigned,
                           SUM(CASE WHEN status='ASSIGNED' THEN 1 ELSE 0 END) AS pending,
                           SUM(CASE WHEN status='IN_PROGRESS' THEN 1 ELSE 0 END) AS in_progress
                    FROM tasks WHERE assignee_id IS NOT NULL GROUP BY assignee_id
                ) t ON t.assignee_id=u.id
                LEFT JOIN (
                    SELECT a.author_id, COUNT(*) AS completed
                    FROM annotations a JOIN tasks t ON t.sample_id=a.task_id
                    WHERE a.submitted=1 AND t.status='SUBMITTED' AND a.author_id IS NOT NULL
                    GROUP BY a.author_id
                ) a ON a.author_id=u.id
                LEFT JOIN (
                    SELECT author_id, COUNT(*) AS ever_completed FROM (
                        SELECT author_id,task_id FROM task_completions
                        UNION
                        SELECT a.author_id, a.task_id FROM annotations a
                        JOIN tasks t ON t.sample_id=a.task_id
                        WHERE a.submitted=1 AND t.status='SUBMITTED'
                              AND a.author_id IS NOT NULL
                        UNION
                        SELECT author_id,task_id FROM annotation_history
                        WHERE submitted=1 AND author_id IS NOT NULL
                    ) submissions GROUP BY author_id
                ) h ON h.author_id=u.id
                ORDER BY u.username
            """).fetchall()
            self.send_json({"users": [dict(r) for r in rows]})
            return
        if path == "/api/admin/users/bulk" and method == "POST":
            users = parse_bulk_users(self.body().get("text"))
            # Hashing 200 passwords inside a write transaction blocks all drafts.
            prepared = [
                (name, email, hash_password(password))
                for name, email, password in users
            ]
            db.execute("BEGIN IMMEDIATE")
            try:
                for username, email, password_hash in prepared:
                    if db.execute(
                        "SELECT 1 FROM users WHERE username=?", (username,)
                    ).fetchone():
                        raise InputError(f"username already exists: {username}")
                    db.execute(
                        "INSERT INTO users(username,email,password_hash,role,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            username,
                            email,
                            password_hash,
                            "ANNOTATOR",
                            int(time.time()),
                        ),
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            self.send_json({"created": len(users)}, 201)
            return
        if path == "/api/admin/users" and method == "POST":
            value = self.body()
            username = value.get("username", "")
            if not isinstance(username, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{3,40}", username
            ):
                raise InputError("username needs 3–40 letters, digits, _, . or -")
            if not isinstance(value.get("password"), str):
                raise InputError("password is required")
            email = validate_email(value.get("email"), required=False)
            password = hash_password(value["password"])
            db.execute(
                "INSERT INTO users(username,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (username, email, password, "ANNOTATOR", int(time.time())),
            )
            self.send_json({"ok": True}, 201)
            return
        if path == "/api/admin/users/update" and method == "POST":
            value = self.body()
            if type(value.get("user_id")) is not int:
                raise InputError("user_id must be an integer")
            if "active" in value and not isinstance(value["active"], bool):
                raise InputError("active must be true or false")
            email = validate_email(value["email"], required=False) if "email" in value else None
            password_hash = hash_password(value["password"]) if "password" in value else None
            target = db.execute(
                "SELECT * FROM users WHERE id=?", (value.get("user_id"),)
            ).fetchone()
            if not target or target["role"] != "ANNOTATOR":
                raise InputError("only annotator accounts can be changed here")
            db.execute("BEGIN IMMEDIATE")
            try:
                if password_hash is not None:
                    db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, target["id"]))
                if "email" in value:
                    db.execute("UPDATE users SET email=? WHERE id=?", (email, target["id"]))
                    db.execute(
                        "UPDATE annotations SET annotator_email=? WHERE author_id=?",
                        (email, target["id"]),
                    )
                if "active" in value:
                    db.execute("UPDATE users SET active=? WHERE id=?", (int(value["active"]), target["id"]))
                if password_hash is not None or value.get("active") is False:
                    db.execute("DELETE FROM sessions WHERE user_id=?", (target["id"],))
                db.commit()
            except Exception:
                db.rollback()
                raise
            self.send_json(
                {
                    "ok": True,
                    "backup_warning": self.record_snapshot()
                    if "email" in value
                    else None,
                }
            )
            return
        if path == "/api/admin/import" and method == "POST":
            value = self.body()
            valid, duplicate, errors, invalid = validate_import(db, value.get("text"))
            result = {
                "valid": len(valid),
                "duplicate": duplicate,
                "invalid": invalid,
                "errors": errors,
            }
            if value.get("commit") is True:
                if invalid:
                    raise InputError(
                        "import has invalid rows; fix them before importing"
                    )
                now = int(time.time())
                inserted = 0
                db.execute("BEGIN IMMEDIATE")
                try:
                    for item in valid:
                        submitted = item["imported_annotation"] is not None
                        db.execute(
                            """
                            INSERT OR IGNORE INTO tasks(
                              sample_id,case_type,split,query_image_id,target_image_id,
                              target_image_ids_json,query_image_path,target_image_path,
                              query_boxes_json,target_boxes_json,candidates_json,
                              initial_subjects_json,status,created_at,updated_at,submitted_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                            (
                                item["sample_id"],
                                item["case_type"],
                                item["split"],
                                item["query_image_id"],
                                item["target_image_id"],
                                json.dumps(item["target_image_ids"]),
                                item["query_image_path"],
                                item["target_image_path"],
                                json.dumps(item["query_boxes"]),
                                json.dumps(item["target_boxes"]),
                                json.dumps(item["candidate_identity_ids"]),
                                json.dumps(item["initial_subjects"]),
                                "SUBMITTED" if submitted else "UNASSIGNED",
                                now,
                                now,
                                now if submitted else None,
                            ),
                        )
                        changed = db.execute("SELECT changes()").fetchone()[0]
                        inserted += changed
                        if submitted and changed:
                            db.execute(
                                """INSERT INTO annotations(
                                task_id,author_id,annotator_email,data_json,submitted,updated_at)
                                VALUES(?,NULL,?,?,1,?)""",
                                (
                                    item["sample_id"],
                                    item["imported_annotator_email"],
                                    json.dumps(item["imported_annotation"]),
                                    now,
                                ),
                            )
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                result["backup_warning"] = self.record_snapshot()
                result["imported"] = inserted
                result["duplicate"] += len(valid) - inserted
            self.send_json(result)
            return
        if path == "/api/admin/tasks" and method == "GET":
            where, params = self.filters(query)
            try:
                limit = min(250, max(1, int(query.get("limit", [100])[0])))
                offset = max(0, int(query.get("offset", [0])[0]))
            except ValueError as exc:
                raise InputError("invalid pagination") from exc
            total = db.execute(
                f"SELECT COUNT(*) FROM tasks t WHERE {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"""
                SELECT t.*,u.username AS assignee FROM tasks t
                LEFT JOIN users u ON u.id=t.assignee_id
                WHERE {where} ORDER BY t.created_at,t.sample_id LIMIT ? OFFSET ?
            """,
                (*params, limit, offset),
            ).fetchall()
            stats = dict(
                db.execute(
                    "SELECT status,COUNT(*) FROM tasks GROUP BY status"
                ).fetchall()
            )
            self.send_json(
                {
                    "tasks": [{**row_task(r), "assignee": r["assignee"]} for r in rows],
                    "total": total,
                    "counts": stats,
                }
            )
            return
        if path == "/api/admin/assign" and method == "POST":
            self.assign(db, self.body())
            return
        if path == "/api/admin/export" and method == "GET":
            where, params = self.filters(query, export=True)
            rows = db.execute(
                f"""SELECT t.*,a.data_json,
                    CASE WHEN a.author_id IS NULL THEN a.annotator_email
                         ELSE u.email END AS annotator_email
                FROM tasks t
                JOIN annotations a ON a.task_id=t.sample_id
                LEFT JOIN users u ON u.id=a.author_id
                WHERE t.status='SUBMITTED' AND a.submitted=1 AND {where}
                ORDER BY t.sample_id""",
                params,
            )
            data = "".join(
                json.dumps(
                    export_record(r), ensure_ascii=False
                )
                + "\n"
                for r in rows
            ).encode("utf-8")
            self.send_file(
                data,
                "application/x-ndjson; charset=utf-8",
                {
                    "Content-Disposition": 'attachment; filename="rcr-submitted.jsonl"',
                    "X-Export-Count": str(data.count(b"\n")),
                },
            )
            return
        raise InputError("unknown admin endpoint")

    def filters(self, query: dict, export: bool = False) -> tuple[str, list]:
        conditions, args = ["1=1"], []
        for field in ("case_type", "split", "status"):
            if field == "status" and export:
                continue
            value = query.get(field, [""])[0]
            if value:
                conditions.append(f"t.{field}=?")
                args.append(value)
        who = query.get("assignee_id", [""])[0]
        if who == "unassigned":
            conditions.append("t.assignee_id IS NULL")
        elif who:
            try:
                args.append(int(who))
            except ValueError as exc:
                raise InputError("invalid assignee_id") from exc
            conditions.append("t.assignee_id=?")
        search = query.get("search", [""])[0].strip()
        if search:
            conditions.append("t.sample_id LIKE ? ESCAPE '\\'")
            args.append(
                "%"
                + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                + "%"
            )
        return " AND ".join(conditions), args

    def assign(self, db: sqlite3.Connection, value: dict) -> None:
        ids = value.get("ids")
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > 2000
            or any(not isinstance(sid, str) or not sid for sid in ids)
            or len(set(ids)) != len(ids)
        ):
            raise InputError("select 1–2,000 distinct task IDs")
        assignee = value.get("assignee_id")
        if assignee is not None:
            if type(assignee) is not int:
                raise InputError("assignee_id must be an integer or null")
        force = value.get("force") is True
        now = int(time.time())
        db.execute("BEGIN IMMEDIATE")
        try:
            if assignee is not None:
                target = db.execute("SELECT role,active FROM users WHERE id=?", (assignee,)).fetchone()
                if not target or target["role"] != "ANNOTATOR" or not target["active"]:
                    raise InputError("choose an active annotator")
            tasks = [
                db.execute("SELECT * FROM tasks WHERE sample_id=?", (sid,)).fetchone()
                for sid in ids
            ]
            if any(t is None for t in tasks):
                raise InputError("one or more tasks no longer exist")
            conflicts = [
                t["sample_id"]
                for t in tasks
                if t["status"] in ("IN_PROGRESS", "SUBMITTED")
                or (t["assignee_id"] is not None and t["assignee_id"] != assignee)
            ]
            if conflicts and not force:
                raise Conflict(
                    f"{len(conflicts)} task(s) already assigned/started/submitted; "
                    "explicit force is required to archive old work and reopen"
                )
            for task in tasks:
                sid = task["sample_id"]
                if sid in conflicts:
                    existing = db.execute(
                        "SELECT * FROM annotations WHERE task_id=?", (sid,)
                    ).fetchone()
                    if existing:
                        db.execute(
                            """INSERT INTO annotation_history(
                            task_id,author_id,annotator_email,data_json,submitted,
                            archived_at,reason)
                            VALUES(?,?,?,?,?,?,?)""",
                            (
                                sid,
                                existing["author_id"],
                                existing["annotator_email"],
                                existing["data_json"],
                                existing["submitted"],
                                now,
                                "admin_reassign",
                            ),
                        )
                        db.execute("DELETE FROM annotations WHERE task_id=?", (sid,))
                db.execute(
                    """UPDATE tasks SET assignee_id=?,status=?,case_type=?,revision=revision+1,
                    started_at=NULL,submitted_at=NULL,reopened=0,updated_at=? WHERE sample_id=?""",
                    (
                        assignee,
                        "ASSIGNED" if assignee is not None else "UNASSIGNED",
                        task["case_type"],
                        now,
                        sid,
                    ),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        self.send_json(
            {
                "updated": len(tasks),
                "archived": len(conflicts),
                "backup_warning": self.record_snapshot(),
            }
        )

    def work(
        self, db: sqlite3.Connection, path: str, method: str, user: sqlite3.Row
    ) -> None:
        if path == "/api/work/tasks" and method == "GET":
            rows = db.execute(
                "SELECT * FROM tasks WHERE assignee_id=? ORDER BY created_at,sample_id",
                (user["id"],),
            ).fetchall()
            self.send_json({"tasks": [row_task(r) for r in rows]})
            return
        match = re.fullmatch(
            r"/api/work/tasks/([^/]+)(?:/(draft|submit|suggest|reopen))?", path
        )
        if not match:
            raise InputError("unknown task endpoint")
        sid, action = unquote(match[1]), match[2]
        if method == "GET" and not action:
            # Metadata and annotation must describe the same committed revision.
            db.execute("BEGIN")
        row = db.execute("SELECT * FROM tasks WHERE sample_id=?", (sid,)).fetchone()
        if not row or row["assignee_id"] != user["id"]:
            raise Forbidden("task is not assigned to you")
        if method == "GET" and not action:
            annotation = db.execute(
                "SELECT data_json FROM annotations WHERE task_id=?", (sid,)
            ).fetchone()
            self.send_json(
                {
                    "task": row_task(row, True),
                    "annotation": json.loads(annotation["data_json"])
                    if annotation
                    else None,
                }
            )
            return
        if method != "POST" or action not in ("draft", "submit", "suggest", "reopen"):
            raise InputError("unknown task action")
        body = self.body()
        candidate_ids = json.loads(row["candidates_json"])
        if action == "reopen":
            if type(body.get("expected_revision")) is not int:
                raise InputError("expected_revision is required")
            now = int(time.time())
            db.execute("BEGIN IMMEDIATE")
            try:
                user = self.require(db, "ANNOTATOR", write=True)
                fresh = db.execute(
                    "SELECT * FROM tasks WHERE sample_id=?", (sid,)
                ).fetchone()
                previous = db.execute(
                    "SELECT * FROM annotations WHERE task_id=?", (sid,)
                ).fetchone()
                if (
                    fresh
                    and fresh["assignee_id"] == user["id"]
                    and fresh["status"] == "IN_PROGRESS"
                    and fresh["reopened"]
                    and fresh["revision"] == body["expected_revision"] + 1
                    and previous
                    and not previous["submitted"]
                ):
                    # Safe retry after the transition committed but its response was lost.
                    db.rollback()
                    result = {
                        "task": row_task(fresh),
                        "annotation": json.loads(previous["data_json"]),
                        "backup_warning": self.record_snapshot(),
                    }
                    self.send_json(result)
                    return
                if not fresh or fresh["assignee_id"] != user["id"]:
                    raise Conflict("task was reassigned; reload")
                if fresh["status"] != "SUBMITTED":
                    raise Conflict("only a submitted task can be reopened")
                if fresh["revision"] != body["expected_revision"]:
                    raise Conflict("task changed in another tab; reload before reopening")
                if not previous or not previous["submitted"]:
                    raise Conflict("submitted task has inconsistent annotation state")
                db.execute(
                    "UPDATE annotations SET submitted=0,updated_at=? WHERE task_id=?",
                    (now, sid),
                )
                db.execute(
                    """UPDATE tasks SET status='IN_PROGRESS',submitted_at=NULL,reopened=1,
                    revision=revision+1,updated_at=? WHERE sample_id=?""",
                    (now, sid),
                )
                saved_task = row_task(
                    db.execute("SELECT * FROM tasks WHERE sample_id=?", (sid,)).fetchone()
                )
                saved_annotation = json.loads(previous["data_json"])
                db.commit()
            except Exception:
                db.rollback()
                raise
            self.send_json(
                {
                    "task": saved_task,
                    "annotation": saved_annotation,
                    "backup_warning": self.record_snapshot(),
                }
            )
            return
        if action == "suggest":
            if row["status"] == "SUBMITTED":
                raise Conflict("submitted tasks are read-only")
            if not (self.gemini_key and self.gemini_model):
                raise InputError(
                    "LLM is not configured; set RCR_GEMINI_API_KEY and RCR_GEMINI_MODEL"
                )
            data = normalize_annotation(body.get("annotation"), candidate_ids, False)
            if any(not subject["identity_ids"] for subject in data["subjects"]):
                raise InputError("select every Subject before asking the LLM")
            if any(not text.strip() for text in data["select_texts"]):
                raise InputError(
                    "write a draft for every SELECT field before using Fix with LLM; Vietnamese notes are allowed"
                )
            if not data["target_condition"].strip():
                raise InputError(
                    "write a TARGET draft before using Fix with LLM; Vietnamese notes are allowed"
                )
            if (
                data["case_type"] == "INDIVIDUAL"
                and len(data["subjects"][0]["identity_ids"]) != 1
            ):
                raise InputError("INDIVIDUAL requires exactly one identity")
            if (
                data["case_type"] == "GROUP"
                and len(data["subjects"][0]["identity_ids"]) < 2
            ):
                raise InputError("GROUP requires at least two identities")
            revision = body.get("expected_revision")
            if type(revision) is not int or revision != row["revision"]:
                raise Conflict("task changed; reload before requesting the LLM")
            note = body.get("note", "")
            if not isinstance(note, str) or len(note) > 500:
                raise InputError("note must be at most 500 characters")
            with self.llm_lock:
                if user["id"] in self.llm_users:
                    raise Conflict("a Gemini request is already running for your account")
                if not self.llm_slots.acquire(blocking=False):
                    raise Conflict("Gemini is busy; retry shortly")
                self.llm_users.add(user["id"])
            try:
                suggestion = gemini_suggestion(
                    self.gemini_model, self.gemini_key,
                    data["case_type"], data["subjects"], data["select_texts"],
                    data["target_condition"], note,
                )
            finally:
                with self.llm_lock:
                    self.llm_users.discard(user["id"])
                    self.llm_slots.release()
            self.require(db, "ANNOTATOR", write=True)
            fresh = db.execute(
                "SELECT assignee_id,status,revision FROM tasks WHERE sample_id=?",
                (sid,),
            ).fetchone()
            if (
                not fresh
                or fresh["assignee_id"] != user["id"]
                or fresh["status"] == "SUBMITTED"
                or fresh["revision"] != revision
            ):
                raise Conflict("task changed while the LLM was working; reload")
            self.send_json({"suggestion": suggestion})
            return
        if type(body.get("expected_revision")) is not int:
            raise InputError("expected_revision is required")
        data = normalize_annotation(
            body.get("annotation"), candidate_ids, complete=(action == "submit")
        )
        now = int(time.time())
        db.execute("BEGIN IMMEDIATE")
        try:
            user = self.require(db, "ANNOTATOR", write=True)
            fresh = db.execute(
                "SELECT * FROM tasks WHERE sample_id=?", (sid,)
            ).fetchone()
            previous = db.execute("SELECT * FROM annotations WHERE task_id=?", (sid,)).fetchone()
            if (
                fresh and fresh["assignee_id"] == user["id"] and previous
                and previous["author_id"] == user["id"]
                and fresh["revision"] == body["expected_revision"] + 1
                and bool(previous["submitted"]) == (action == "submit")
                and json.loads(previous["data_json"]) == data
            ):
                # The client may retry after the commit succeeded but its reply was lost.
                db.rollback()
                result = {"task": row_task(fresh), "annotation": data}
                if action == "submit":
                    result["backup_warning"] = self.record_snapshot()
                self.send_json(result)
                return
            if not fresh or fresh["assignee_id"] != user["id"] or fresh["status"] == "SUBMITTED":
                raise Conflict("task was reassigned or already submitted; reload")
            if fresh["revision"] != body["expected_revision"]:
                raise Conflict("task changed in another tab; reload before saving")
            db.execute(
                """INSERT INTO annotations(
                task_id,author_id,annotator_email,data_json,submitted,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                author_id=excluded.author_id,annotator_email=excluded.annotator_email,
                data_json=excluded.data_json,submitted=excluded.submitted,
                updated_at=excluded.updated_at""",
                (
                    sid,
                    user["id"],
                    user["email"],
                    json.dumps(data, ensure_ascii=False),
                    int(action == "submit"),
                    now,
                ),
            )
            db.execute(
                """UPDATE tasks SET case_type=?,status=?,revision=revision+1,
                started_at=COALESCE(started_at,?),submitted_at=?,updated_at=?
                WHERE sample_id=?""",
                (
                    data["case_type"],
                    "SUBMITTED" if action == "submit" else "IN_PROGRESS",
                    now,
                    now if action == "submit" else None,
                    now,
                    sid,
                ),
            )
            if action == "submit":
                db.execute("UPDATE tasks SET reopened=0 WHERE sample_id=?", (sid,))
                db.execute("INSERT OR IGNORE INTO task_completions(task_id,author_id) VALUES(?,?)",
                           (sid, user["id"]))
            saved_task = row_task(db.execute("SELECT * FROM tasks WHERE sample_id=?", (sid,)).fetchone())
            db.commit()
        except Exception:
            db.rollback()
            raise
        result = {
            "task": saved_task,
            "annotation": data,
        }
        if action == "submit":
            result["backup_warning"] = self.record_snapshot()
        self.send_json(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal RCR admin and annotation web app"
    )
    parser.add_argument("command", choices=["serve", "init-admin", "backup", "check"])
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("rcr-data.sqlite3"))
    parser.add_argument("--out", type=Path, help="new SQLite file for backup")
    parser.add_argument("--username", help="username for init-admin")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    args.db = args.db.expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command in ("backup", "check"):
        if not args.db.is_file():
            parser.error(f"database does not exist: {args.db}")
        if args.command == "backup":
            if not args.out:
                parser.error("backup requires --out NEW_BACKUP.sqlite3")
            try:
                print(f"Backup saved: {backup_database(args.db, args.out.expanduser())}")
            except (OSError, sqlite3.Error) as exc:
                parser.error(str(exc))
        else:
            with closing(sqlite3.connect(args.db.as_uri() + "?mode=ro", uri=True)) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
                counts = dict(db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status"))
                state_errors = db.execute("""
                    SELECT COUNT(*) FROM tasks t
                    LEFT JOIN annotations a ON a.task_id=t.sample_id
                    WHERE (t.status='SUBMITTED' AND
                           (a.task_id IS NULL OR a.submitted!=1 OR t.submitted_at IS NULL))
                       OR (t.status='IN_PROGRESS' AND
                           (a.task_id IS NULL OR a.submitted!=0 OR t.submitted_at IS NOT NULL))
                       OR (t.status='ASSIGNED' AND
                           (t.assignee_id IS NULL OR a.task_id IS NOT NULL
                            OR t.submitted_at IS NOT NULL))
                       OR (t.status='UNASSIGNED' AND
                           (t.assignee_id IS NOT NULL OR a.task_id IS NOT NULL
                            OR t.submitted_at IS NOT NULL))
                """).fetchone()[0]
                print(json.dumps({"database": str(args.db), "integrity": integrity,
                                  "foreign_key_errors": len(foreign_keys),
                                  "state_invariant_errors": state_errors,
                                  "tasks": counts}, indent=2))
                if integrity != "ok" or foreign_keys or state_errors:
                    raise SystemExit(1)
        return
    init_database(args.db)
    if args.command == "init-admin":
        if not args.username or not re.fullmatch(
            r"[A-Za-z0-9_.-]{3,40}", args.username
        ):
            parser.error("--username needs 3–40 letters, digits, _, . or -")
        password = getpass.getpass("New admin password (at least 4 characters): ")
        again = getpass.getpass("Confirm password: ")
        if password != again:
            parser.error("passwords do not match")
        try:
            with closing(connect(args.db)) as db:
                db.execute(
                    "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                    (args.username, hash_password(password), "ADMIN", int(time.time())),
                )
        except (InputError, sqlite3.IntegrityError) as exc:
            parser.error(str(exc))
        print("Admin account created")
        return
    image_root = (
        Path(os.environ["RCR_IMAGE_ROOT"]).expanduser().resolve() if os.getenv("RCR_IMAGE_ROOT") else None
    )
    if image_root is not None and not image_root.is_dir():
        parser.error("RCR_IMAGE_ROOT must point to an existing image directory")
    if image_root is None:
        LOG.warning("RCR_IMAGE_ROOT is not set; task images will be unavailable")
    handler = type(
        "ConfiguredHandler",
        (Handler,),
        {
            "db_path": args.db,
            "image_root": image_root,
            "gemini_key": os.getenv("RCR_GEMINI_API_KEY", ""),
            "gemini_model": os.getenv("RCR_GEMINI_MODEL", ""),
            "cookie_secure": os.getenv("RCR_COOKIE_SECURE") == "1",
        },
    )
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            parser.error(f"Port {args.port} is already in use; stop the old server or choose another --port")
        parser.error(str(exc))
    try:
        write_snapshot(args.db)
    except (OSError, sqlite3.Error):
        LOG.exception("Could not refresh final JSONL at startup; SQLite remains available")
    print(f"Database: {args.db}")
    print(f"RCR web: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
