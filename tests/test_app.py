"""Run from rcr-simple with `python -m unittest discover -s tests -v`."""

from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core import InputError, normalize_annotation, normalize_task, parse_json_tasks
from persistence import backup_database, snapshot_path, write_snapshot
from server import Handler, connect, gemini_suggestion, hash_password, init_database

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
)


def sample(sid: str, case: str = "INDIVIDUAL") -> dict:
    box1 = {"identity_id": "1", "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.3}
    box2 = {"identity_id": "2", "x": 0.5, "y": 0.1, "width": 0.2, "height": 0.3}
    return {
        "sample_id": sid,
        "case_type": case,
        "split": "TRAIN",
        "query_image_id": "q",
        "target_image_id": "t",
        "target_image_ids": ["t", "t2"],
        "query_image_path": "train/q.png",
        "target_image_path": "train/t.png",
        "query_boxes": [box1, box2],
        "target_boxes": [box1, box2],
    }


def annotation(
    case: str,
    ids1: list[str],
    ids2: list[str] | None = None,
    target: str = "Subject 1 is holding a diploma",
) -> dict:
    subjects = [{"subject_id": 1, "identity_ids": ids1}]
    texts = ["the man in black"]
    if ids2 is not None:
        subjects.append({"subject_id": 2, "identity_ids": ids2})
        texts.append("the woman in white")
    return {
        "case_type": case,
        "subjects": subjects,
        "select_texts": texts,
        "target_condition": target,
    }


class ValidationTests(unittest.TestCase):
    def test_import_canonical_text_bom_and_line_numbers(self):
        row = sample("canonical")
        final = normalize_annotation(annotation("INDIVIDUAL", ["1"]), ["1", "2"], True)
        row.update({key: final[key] for key in ("subjects", "final_desc", "final_change")})
        parsed = parse_json_tasks("\ufeff" + json.dumps(row))
        normalized = normalize_task(parsed[0])
        self.assertEqual(normalized["imported_annotation"], final)
        with self.assertRaisesRegex(InputError, "line 3"):
            parse_json_tasks(json.dumps(row) + "\n\n{'not': 'json'}")
        with self.assertRaises(InputError):
            parse_json_tasks("[]")

    def test_reject_broken_initial_subjects_and_nonexistent_reference(self):
        for subjects in [[None], [{"subject_id": 1, "identity_ids": ["unknown"]}], "bad"]:
            row = sample("bad"); row["subjects"] = subjects
            with self.assertRaises(InputError):
                normalize_task(row)
        for target in ("Subject 1 stands near Subject 2", "Subject 1 holds Subject 3"):
            with self.assertRaises(InputError):
                normalize_annotation(annotation("INDIVIDUAL", ["1"], target=target), ["1", "2"], True)

    def test_cases_and_exact_canonical_output(self):
        candidates = ["1", "2"]
        for case, ids, other, target in [
            ("INDIVIDUAL", ["1"], None, "Subject 1 is seated"),
            ("GROUP", ["1", "2"], None, "Members of Subject 1 are seated"),
            ("DUAL", ["1"], ["2"], "Subject 1 is seated and Subject 2 is standing"),
            ("RELATIONAL", ["1"], ["2"], "Subject 1 gives a diploma to Subject 2"),
        ]:
            result = normalize_annotation(
                annotation(case, ids, other, target), candidates, True
            )
            self.assertEqual(
                result["final_change"], "then retrieve target images where " + target
            )
            self.assertEqual(
                result["final_instruction"],
                result["final_desc"] + "; " + result["final_change"] + ".",
            )

    def test_reject_duplicate_identity_and_bad_case(self):
        with self.assertRaises(InputError):
            normalize_annotation(
                annotation("DUAL", ["1"], ["1"], "Subject 1 talks to Subject 2"),
                ["1", "2"],
                True,
            )
        with self.assertRaises(InputError):
            normalize_annotation(annotation("GROUP", ["1"]), ["1", "2"], True)
        with self.assertRaises(InputError):
            normalize_annotation(annotation("SINGLE", ["1"]), ["1", "2"], True)

    def test_no_double_prefix_and_no_path_traversal(self):
        result = normalize_annotation(
            annotation(
                "INDIVIDUAL",
                ["1"],
                target="then retrieve target images where Subject 1 is seated.",
            ),
            ["1", "2"],
            True,
        )
        self.assertEqual(
            result["final_change"],
            "then retrieve target images where Subject 1 is seated",
        )
        row = sample("x")
        row["query_image_path"] = "../secrets.png"
        with self.assertRaises(InputError):
            normalize_task(row)

    def test_llm_request_contains_two_images_and_returns_only_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(ONE_PIXEL_PNG)
            fake = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "internal reasoning", "thought": True},
                                {
                                    "text": json.dumps(
                                        {
                                            "select_texts": ["the man", "the woman"],
                                            "target_condition": "Subject 1 is presenting a diploma to Subject 2",
                                        }
                                    )
                                },
                            ]
                        }
                    }
                ]
            }
            with patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(fake).encode()),
            ) as call:
                suggestion = gemini_suggestion(
                    "configured-model",
                    "test-key",
                    path,
                    path,
                    "RELATIONAL",
                    [
                        {"subject_id": 1, "identity_ids": ["1"]},
                        {"subject_id": 2, "identity_ids": ["2"]},
                    ],
                    ["the man", "the woman"],
                    "",
                    "",
                    [],
                    [],
                )
            request = call.call_args.args[0]
            payload = json.loads(request.data)
            parts = payload["contents"][0]["parts"]
            self.assertEqual(sum("inline_data" in p for p in parts), 2)
            self.assertEqual(
                payload["generationConfig"]["responseMimeType"], "application/json"
            )
            self.assertEqual(
                payload["generationConfig"]["responseSchema"]["required"],
                ["select_texts", "target_condition"],
            )
            self.assertGreaterEqual(
                payload["generationConfig"]["maxOutputTokens"], 4096
            )
            self.assertEqual(
                suggestion["target_condition"],
                "Subject 1 is presenting a diploma to Subject 2",
            )
            self.assertEqual(suggestion["select_texts"], ["the man", "the woman"])

    def test_gemini_truncated_response_and_missing_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(ONE_PIXEL_PNG)
            cases = [
                ({"candidates": [{"finishReason": "MAX_TOKENS"}]}, "truncated"),
                ({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}, "invalid"),
            ]
            for response, message in cases:
                with (
                    self.subTest(message=message),
                    patch(
                        "urllib.request.urlopen",
                        return_value=io.BytesIO(json.dumps(response).encode()),
                    ),
                ):
                    with self.assertRaisesRegex(InputError, message):
                        gemini_suggestion(
                            "model",
                            "key",
                            path,
                            path,
                            "INDIVIDUAL",
                            [{"subject_id": 1, "identity_ids": ["1"]}],
                            [""],
                            "",
                            "",
                            [],
                            [],
                        )

    def test_llm_cannot_change_subject_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(ONE_PIXEL_PNG)
            fake = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "select_texts": ["the man"],
                                            "target_condition": "A different person is seated",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
            with patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(fake).encode()),
            ):
                with self.assertRaisesRegex(InputError, "LLM output is invalid"):
                    gemini_suggestion(
                        "model",
                        "key",
                        path,
                        path,
                        "INDIVIDUAL",
                        [{"subject_id": 1, "identity_ids": ["1"]}],
                        [""],
                        "",
                        "",
                        [],
                        [],
                    )


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "data.sqlite3"
        self.images = root / "images"
        (self.images / "train").mkdir(parents=True)
        for name in ("q.png", "t.png"):
            (self.images / "train" / name).write_bytes(ONE_PIXEL_PNG)
        init_database(self.db)
        with connect(self.db) as db:
            db.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                (
                    "admin",
                    hash_password("strong admin pass"),
                    "ADMIN",
                    int(time.time()),
                ),
            )
        bound = type(
            "BoundHandler",
            (Handler,),
            {
                "db_path": self.db,
                "image_root": self.images,
                "gemini_key": "",
                "gemini_model": "",
                "cookie_secure": False,
                "log_message": lambda *args: None,
            },
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.admin = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self.worker = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self.stranger = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self.admin_csrf = self.login(self.admin, "admin", "strong admin pass")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, opener, path, value=None, csrf=None):
        body = json.dumps(value).encode() if value is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers=headers,
            method="POST" if body is not None else "GET",
        )
        try:
            with opener.open(request) as response:
                result = response.read()
                return (
                    response.status,
                    json.loads(result)
                    if response.headers.get_content_type() == "application/json"
                    else result,
                )
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def login(self, opener, username, password):
        status, data = self.request(
            opener, "/api/auth/login", {"username": username, "password": password}
        )
        self.assertEqual(status, 200, data)
        return data["csrf"]

    def test_bulk_users_atomic_and_admin_only(self):
        endpoint = "/api/admin/users/bulk"
        status, _ = self.request(
            self.stranger, endpoint, {"text": "workerA,strong password 1"}
        )
        self.assertEqual(status, 401)
        status, _ = self.request(
            self.admin, endpoint, {"text": "workerA,strong password 1"}
        )
        self.assertEqual(status, 403)
        for text in (
            "workerA,strong password 1\nworkerA,strong password 2",
            "workerA,strong password 1\nworkerB,abc",
            "workerA,strong password 1\ninvalid-name!,strong password 2",
        ):
            status, _ = self.request(
                self.admin, endpoint, {"text": text}, self.admin_csrf
            )
            self.assertEqual(status, 422)
            with connect(self.db) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1
                )
        status, result = self.request(
            self.admin,
            endpoint,
            {
                "text": 'workerA,"strong,password 1"\nworkerB,strong password 2\n',
            },
            self.admin_csrf,
        )
        self.assertEqual((status, result["created"]), (201, 2))
        self.login(self.worker, "workerA", "strong,password 1")
        status, _ = self.request(
            self.admin,
            endpoint,
            {
                "text": "workerC,strong password 3\nworkerB,strong password 2",
            },
            self.admin_csrf,
        )
        self.assertEqual(status, 422)
        with connect(self.db) as db:
            self.assertIsNone(
                db.execute("SELECT id FROM users WHERE username='workerC'").fetchone()
            )

    def test_user_stats_follow_submission_and_reassignment(self):
        endpoint = "/api/admin/users/bulk"
        self.request(
            self.admin,
            endpoint,
            {
                "text": "workerA,strong password 1\nworkerB,strong password 2",
            },
            self.admin_csrf,
        )
        imported = sample("legacy")
        imported["imported_annotation"] = annotation("INDIVIDUAL", ["1"])
        text = "\n".join(
            json.dumps(item)
            for item in (
                sample("pending"),
                sample("done"),
                imported,
            )
        )
        self.request(
            self.admin,
            "/api/admin/import",
            {"text": text, "commit": True},
            self.admin_csrf,
        )
        status, response = self.request(self.admin, "/api/admin/users")
        first = next(u for u in response["users"] if u["username"] == "workerA")
        second = next(u for u in response["users"] if u["username"] == "workerB")
        self.assertEqual(
            (
                first["assigned"],
                first["pending"],
                first["in_progress"],
                first["completed"],
                first["ever_completed"],
            ),
            (0, 0, 0, 0, 0),
        )
        self.request(
            self.admin,
            "/api/admin/assign",
            {
                "ids": ["pending", "done"],
                "assignee_id": first["id"],
            },
            self.admin_csrf,
        )
        worker_csrf = self.login(self.worker, "workerA", "strong password 1")
        _, task = self.request(self.worker, "/api/work/tasks/pending")
        self.request(
            self.worker,
            "/api/work/tasks/pending/draft",
            {
                "expected_revision": task["task"]["revision"],
                "annotation": annotation("INDIVIDUAL", ["1"]),
            },
            worker_csrf,
        )
        _, task = self.request(self.worker, "/api/work/tasks/done")
        self.request(
            self.worker,
            "/api/work/tasks/done/submit",
            {
                "expected_revision": task["task"]["revision"],
                "annotation": annotation("INDIVIDUAL", ["1"]),
            },
            worker_csrf,
        )
        _, response = self.request(self.admin, "/api/admin/users")
        first = next(u for u in response["users"] if u["id"] == first["id"])
        self.assertEqual(
            (
                first["assigned"],
                first["pending"],
                first["in_progress"],
                first["completed"],
                first["ever_completed"],
            ),
            (2, 0, 1, 1, 1),
        )
        self.assertEqual(
            next(u for u in response["users"] if u["id"] == second["id"])["completed"],
            0,
        )
        self.request(
            self.admin,
            "/api/admin/assign",
            {
                "ids": ["done"],
                "assignee_id": second["id"],
                "force": True,
            },
            self.admin_csrf,
        )
        _, response = self.request(self.admin, "/api/admin/users")
        first = next(u for u in response["users"] if u["id"] == first["id"])
        second = next(u for u in response["users"] if u["id"] == second["id"])
        self.assertEqual(
            (first["assigned"], first["completed"], first["ever_completed"]), (1, 0, 1)
        )
        self.assertEqual(
            (
                second["assigned"],
                second["pending"],
                second["completed"],
                second["ever_completed"],
            ),
            (1, 1, 0, 0),
        )
        with connect(self.db) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM annotation_history WHERE submitted=1 AND author_id=?",
                    (first["id"],),
                ).fetchone()[0],
                1,
            )

    def test_full_flow_ownership_revision_export_and_history(self):
        for path, marker in (
            ("/", b"TARGET CONDITION"),
            ("/app.js", b"function renderWork"),
            ("/style.css", b".work-layout"),
        ):
            status, content = self.request(self.admin, path)
            self.assertEqual(status, 200)
            self.assertIn(marker, content)
        tasks = [sample("x"), sample("y")]
        text = "\n".join(json.dumps(t) for t in tasks)
        status, preview = self.request(
            self.admin,
            "/api/admin/import",
            {"text": text, "commit": False},
            self.admin_csrf,
        )
        self.assertEqual((status, preview["valid"], preview["invalid"]), (200, 2, 0))
        status, _ = self.request(
            self.admin,
            "/api/admin/import",
            {"text": text, "commit": True},
            self.admin_csrf,
        )
        self.assertEqual(status, 200)
        status, data = self.request(
            self.admin, "/api/admin/import", {"text": text}, self.admin_csrf
        )
        self.assertEqual(data["duplicate"], 2)
        status, _ = self.request(
            self.admin,
            "/api/admin/users",
            {"username": "worker1", "password": "strong worker pass"},
            self.admin_csrf,
        )
        self.assertEqual(status, 201)
        status, users = self.request(self.admin, "/api/admin/users")
        uid = next(u["id"] for u in users["users"] if u["username"] == "worker1")
        status, _ = self.request(
            self.admin,
            "/api/admin/assign",
            {"ids": ["x"], "assignee_id": uid},
            self.admin_csrf,
        )
        self.assertEqual(status, 200)
        self.worker_csrf = self.login(self.worker, "worker1", "strong worker pass")
        status, data = self.request(self.worker, "/api/work/tasks")
        self.assertEqual([t["sample_id"] for t in data["tasks"]], ["x"])
        status, _ = self.request(self.worker, "/api/work/tasks/y")
        self.assertEqual(status, 403)
        status, _ = self.request(self.worker, "/api/image/y/query")
        self.assertEqual(status, 403)
        status, image = self.request(self.worker, "/api/image/x/query")
        self.assertEqual((status, image), (200, ONE_PIXEL_PNG))
        status, data = self.request(self.worker, "/api/work/tasks/x")
        revision = data["task"]["revision"]
        payload = {
            "expected_revision": revision,
            "annotation": annotation("INDIVIDUAL", ["1"]),
        }
        status, _ = self.request(
            self.worker,
            "/api/work/tasks/x/suggest",
            {"annotation": payload["annotation"]},
            self.worker_csrf,
        )
        self.assertEqual(status, 422)  # LLM is optional; manual annotation still works.
        status, saved = self.request(
            self.worker, "/api/work/tasks/x/draft", payload, self.worker_csrf
        )
        self.assertEqual((status, saved["task"]["status"]), (200, "IN_PROGRESS"))
        self.assertEqual(
            snapshot_path(self.db).read_text(), ""
        )  # Drafts live in SQLite.
        status, _ = self.request(
            self.worker, "/api/work/tasks/x/draft", payload, self.worker_csrf
        )
        self.assertEqual(status, 200)  # Identical retry after a lost response is safe.
        stale = {**payload, "annotation": annotation("INDIVIDUAL", ["2"])}
        status, _ = self.request(
            self.worker, "/api/work/tasks/x/draft", stale, self.worker_csrf
        )
        self.assertEqual(status, 409)
        status, _ = self.request(
            self.worker,
            "/api/work/tasks/x/submit",
            {
                "expected_revision": saved["task"]["revision"],
                "annotation": payload["annotation"],
            },
        )
        self.assertEqual(status, 403)  # cannot submit without CSRF token
        status, submitted = self.request(
            self.worker,
            "/api/work/tasks/x/submit",
            {
                "expected_revision": saved["task"]["revision"],
                "annotation": payload["annotation"],
            },
            self.worker_csrf,
        )
        self.assertEqual((status, submitted["task"]["status"]), (200, "SUBMITTED"))
        status, content = self.request(self.admin, "/api/admin/export")
        self.assertEqual(status, 200)
        row = json.loads(content.decode().splitlines()[0])
        self.assertEqual(snapshot_path(self.db).read_bytes(), content)
        self.assertEqual(row["target_image_ids"], ["t", "t2"])
        self.assertEqual(
            row["final_instruction"],
            row["final_desc"] + "; " + row["final_change"] + ".",
        )
        status, _ = self.request(
            self.admin,
            "/api/admin/assign",
            {"ids": ["x"], "assignee_id": None},
            self.admin_csrf,
        )
        self.assertEqual(status, 409)
        status, result = self.request(
            self.admin,
            "/api/admin/assign",
            {"ids": ["x"], "assignee_id": None, "force": True},
            self.admin_csrf,
        )
        self.assertEqual(result["archived"], 1)
        with connect(self.db) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM annotation_history").fetchone()[0], 1
            )
        self.assertEqual(snapshot_path(self.db).read_text(), "")

    def test_llm_temporary_suggestion_and_revision(self):
        text = json.dumps(sample("fix"))
        self.request(
            self.admin,
            "/api/admin/import",
            {"text": text, "commit": True},
            self.admin_csrf,
        )
        self.request(
            self.admin,
            "/api/admin/users",
            {"username": "worker1", "password": "strong worker pass"},
            self.admin_csrf,
        )
        status, users = self.request(self.admin, "/api/admin/users")
        uid = next(u["id"] for u in users["users"] if u["username"] == "worker1")
        self.request(
            self.admin,
            "/api/admin/assign",
            {"ids": ["fix"], "assignee_id": uid},
            self.admin_csrf,
        )
        csrf = self.login(self.worker, "worker1", "strong worker pass")
        status, fetched = self.request(self.worker, "/api/work/tasks/fix")
        revision = fetched["task"]["revision"]
        self.server.RequestHandlerClass.gemini_key = "test-key"
        self.server.RequestHandlerClass.gemini_model = "test-model"
        proposed = {
            "select_texts": ["the man in black"],
            "target_condition": "Subject 1 is holding a diploma",
            "final_instruction": "sample",
        }
        with patch("server.gemini_suggestion", return_value=proposed) as model:
            status, result = self.request(
                self.worker,
                "/api/work/tasks/fix/suggest",
                {
                    "expected_revision": revision,
                    "annotation": annotation("INDIVIDUAL", ["1"], target=""),
                },
                csrf,
            )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["suggestion"], proposed)
        self.assertEqual(model.call_count, 1)
        status, fetched = self.request(self.worker, "/api/work/tasks/fix")
        self.assertNotIn("suggestion", fetched)
        self.assertEqual(snapshot_path(self.db).read_text(), "")
        with patch("server.gemini_suggestion") as model:
            status, stale = self.request(
                self.worker,
                "/api/work/tasks/fix/suggest",
                {
                    "expected_revision": 123,
                    "annotation": annotation("INDIVIDUAL", ["1"], target=""),
                },
                csrf,
            )
        self.assertEqual(status, 409, stale)
        model.assert_not_called()

        def change_while_gemini_runs(*_args):
            with connect(self.db) as db:
                db.execute("UPDATE tasks SET revision=revision+1 WHERE sample_id='fix'")
            return proposed

        with patch("server.gemini_suggestion", side_effect=change_while_gemini_runs):
            status, stale = self.request(
                self.worker,
                "/api/work/tasks/fix/suggest",
                {
                    "expected_revision": revision,
                    "annotation": annotation("INDIVIDUAL", ["1"], target=""),
                },
                csrf,
            )
        self.assertEqual(status, 409, stale)
        revision += 1
        status, result = self.request(
            self.worker,
            "/api/work/tasks/fix/draft",
            {
                "expected_revision": revision,
                "annotation": annotation("INDIVIDUAL", ["1"]),
            },
            csrf,
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(snapshot_path(self.db).read_text(), "")
        with patch("server.write_snapshot", side_effect=OSError("disk full")):
            status, submitted = self.request(
                self.worker,
                "/api/work/tasks/fix/submit",
                {
                    "expected_revision": result["task"]["revision"],
                    "annotation": annotation("INDIVIDUAL", ["1"]),
                },
                csrf,
            )
        self.assertEqual(status, 200, submitted)
        self.assertIn("JSONL", submitted["backup_warning"])
        self.assertEqual(snapshot_path(self.db).read_text(), "")
        write_snapshot(self.db)
        rows = [
            json.loads(line) for line in snapshot_path(self.db).read_text().splitlines()
        ]
        self.assertEqual(
            rows[0]["final_change"],
            "then retrieve target images where " + proposed["target_condition"],
        )

    def test_imported_submitted_task_appears_in_final_jsonl(self):
        existing = sample("already-done")
        existing["imported_annotation"] = annotation("INDIVIDUAL", ["1"])
        status, result = self.request(
            self.admin,
            "/api/admin/import",
            {
                "text": json.dumps(existing),
                "commit": True,
            },
            self.admin_csrf,
        )
        self.assertEqual(status, 200, result)
        rows = [
            json.loads(line) for line in snapshot_path(self.db).read_text().splitlines()
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_id"], "already-done")
        self.assertEqual(
            rows[0]["final_instruction"],
            rows[0]["final_desc"] + "; " + rows[0]["final_change"] + ".",
        )
        original = snapshot_path(self.db).read_bytes()
        with patch("persistence.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                write_snapshot(self.db)
        self.assertEqual(snapshot_path(self.db).read_bytes(), original)

    def prepare_worker(self, ids):
        status, result = self.request(self.admin, "/api/admin/users",
            {"username": "stable", "password": "password"}, self.admin_csrf)
        self.assertEqual(status, 201, result)
        _, users = self.request(self.admin, "/api/admin/users")
        uid = next(u["id"] for u in users["users"] if u["username"] == "stable")
        status, result = self.request(self.admin, "/api/admin/import",
            {"text": json.dumps([sample(sid) for sid in ids]), "commit": True}, self.admin_csrf)
        self.assertEqual(status, 200, result)
        status, result = self.request(self.admin, "/api/admin/assign",
            {"ids": ids, "assignee_id": uid}, self.admin_csrf)
        self.assertEqual(status, 200, result)
        self.worker_csrf = self.login(self.worker, "stable", "password")
        return uid

    def test_concurrent_writes_conflict_and_idempotent_submit(self):
        self.prepare_worker(["race"])
        _, task = self.request(self.worker, "/api/work/tasks/race")
        revision = task["task"]["revision"]
        barrier = threading.Barrier(2)
        def save(identity):
            barrier.wait(timeout=5)
            return self.request(self.worker, "/api/work/tasks/race/draft",
                {"expected_revision": revision, "annotation": annotation("INDIVIDUAL", [identity])}, self.worker_csrf)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["1", "2"]))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(result for status, result in results if status == 200)
        payload = {"expected_revision": winner["task"]["revision"], "annotation": winner["annotation"]}
        status, first = self.request(self.worker, "/api/work/tasks/race/submit", payload, self.worker_csrf)
        self.assertEqual(status, 200, first)
        status, retry = self.request(self.worker, "/api/work/tasks/race/submit", payload, self.worker_csrf)
        self.assertEqual(status, 200, retry)
        self.assertEqual(first["task"]["revision"], retry["task"]["revision"])
        self.assertEqual(len(snapshot_path(self.db).read_text().splitlines()), 1)

    def test_parallel_submissions_and_live_backup(self):
        ids = [f"parallel-{i}" for i in range(30)]
        self.prepare_worker(ids)
        def submit(sid):
            return self.request(self.worker, f"/api/work/tasks/{sid}/submit",
                {"expected_revision": 1, "annotation": annotation("INDIVIDUAL", ["1"])}, self.worker_csrf)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(submit, ids))
        for status, result in results:
            self.assertEqual(status, 200, result)
            self.assertIsNone(result.get("backup_warning"))
        destination = Path(self.temp.name) / "backup.sqlite3"
        backup_database(self.db, destination)
        with connect(destination) as backup:
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM annotations WHERE submitted=1").fetchone()[0], 30)
            self.assertGreater(backup.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        with self.assertRaises(FileExistsError):
            backup_database(self.db, destination)
        _, exported = self.request(self.admin, "/api/admin/export")
        self.assertEqual(snapshot_path(self.db).read_bytes(), exported)
        self.assertEqual(len(exported.splitlines()), 30)

    def test_invalid_user_update_is_atomic_and_bad_ids_are_422(self):
        uid = self.prepare_worker(["valid"])
        status, _ = self.request(self.admin, "/api/admin/users/update",
            {"user_id": uid, "password": "changed", "active": "no"}, self.admin_csrf)
        self.assertEqual(status, 422)
        self.login(self.worker, "stable", "password")
        for value in ({"ids": [{}]}, {"ids": ["valid"], "assignee_id": []}):
            status, result = self.request(self.admin, "/api/admin/assign", value, self.admin_csrf)
            self.assertEqual(status, 422, result)

    def test_canonical_import_preserves_completed_work(self):
        row = sample("already-written")
        final = normalize_annotation(annotation("GROUP", ["1", "2"]), ["1", "2"], True)
        row.update({key: final[key] for key in ("case_type", "subjects", "final_desc", "final_change")})
        status, result = self.request(self.admin, "/api/admin/import",
            {"text": json.dumps(row), "commit": True}, self.admin_csrf)
        self.assertEqual((status, result["imported"]), (200, 1))
        _, tasks = self.request(self.admin, "/api/admin/tasks?case_type=GROUP")
        self.assertEqual(tasks["tasks"][0]["status"], "SUBMITTED")
        _, exported = self.request(self.admin, "/api/admin/export")
        self.assertEqual(json.loads(exported)["final_desc"], final["final_desc"])


if __name__ == "__main__":
    unittest.main()
