"""End-to-end regressions for deployment audit (temporary DBs only)."""
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_app as fixtures
from convert_dataset import build_rows
from core import InputError, normalize_annotation, normalize_task
from server import Handler, connect, gemini_suggestion, init_database


class AuditServerTests(unittest.TestCase):
    setUp = fixtures.ServerTests.setUp
    tearDown = fixtures.ServerTests.tearDown
    request = fixtures.ServerTests.request
    login = fixtures.ServerTests.login
    prepare_worker = fixtures.ServerTests.prepare_worker

    def submit_and_reopen(self):
        uid = self.prepare_worker(["redo"])
        _, item = self.request(self.worker, "/api/work/tasks/redo")
        status, result = self.request(self.worker, "/api/work/tasks/redo/submit", {
            "expected_revision": item["task"]["revision"],
            "annotation": fixtures.annotation("INDIVIDUAL", ["1"]),
        }, self.worker_csrf)
        self.assertEqual(status, 200, result)
        status, result = self.request(self.worker, "/api/work/tasks/redo/reopen", {
            "expected_revision": result["task"]["revision"],
        }, self.worker_csrf)
        self.assertEqual(status, 200, result)
        return uid, result

    def test_reopened_survives_draft_reload_restart_and_resets_on_reassign(self):
        uid, result = self.submit_and_reopen()
        self.assertTrue(result["task"].get("reopened"))
        changed = fixtures.annotation("INDIVIDUAL", ["1"], target="Subject 1 is standing")
        status, result = self.request(self.worker, "/api/work/tasks/redo/draft", {
            "expected_revision": result["task"]["revision"], "annotation": changed,
        }, self.worker_csrf)
        self.assertEqual(status, 200, result)
        init_database(self.db)
        _, result = self.request(self.worker, "/api/work/tasks/redo")
        self.assertTrue(result["task"]["reopened"])
        self.assertEqual(result["annotation"]["target_condition"], "Subject 1 is standing")
        self.request(self.admin, "/api/admin/assign", {
            "ids": ["redo"], "assignee_id": uid, "force": True,
        }, self.admin_csrf)
        _, result = self.request(self.worker, "/api/work/tasks/redo")
        self.assertFalse(result["task"]["reopened"])
        self.assertIsNone(result["annotation"])

    def test_ever_completed_does_not_drop_or_double_count_after_reopen(self):
        uid, result = self.submit_and_reopen()
        _, users = self.request(self.admin, "/api/admin/users")
        user = next(u for u in users["users"] if u["id"] == uid)
        self.assertEqual((user["completed"], user["ever_completed"]), (0, 1))
        status, result = self.request(self.worker, "/api/work/tasks/redo/submit", {
            "expected_revision": result["task"]["revision"],
            "annotation": fixtures.annotation("INDIVIDUAL", ["1"]),
        }, self.worker_csrf)
        self.assertEqual(status, 200, result)
        self.assertFalse(result["task"]["reopened"])
        _, users = self.request(self.admin, "/api/admin/users")
        user = next(u for u in users["users"] if u["id"] == uid)
        self.assertEqual((user["completed"], user["ever_completed"]), (1, 1))
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM annotation_history").fetchone()[0], 0)

    def test_migration_recovers_legacy_reopen_without_overwriting_annotation(self):
        uid, result = self.submit_and_reopen()
        with connect(self.db) as db:
            original = db.execute("SELECT data_json FROM annotations").fetchone()[0]
            db.execute("DROP TABLE task_completions")
            db.execute("ALTER TABLE tasks DROP COLUMN reopened")
        init_database(self.db)
        init_database(self.db)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT reopened FROM tasks").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT data_json FROM annotations").fetchone()[0], original)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_completions").fetchone()[0], 1)

    def test_account_locked_while_body_is_read_cannot_commit(self):
        uid = self.prepare_worker(["locked"])
        _, result = self.request(self.worker, "/api/work/tasks/locked")
        entered, release = threading.Event(), threading.Event()
        original_body = Handler.body

        def paused_body(handler):
            data = original_body(handler)
            if handler.path.endswith("/draft"):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test barrier timed out")
            return data

        with patch.object(Handler, "body", paused_body), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.request, self.worker, "/api/work/tasks/locked/draft", {
                "expected_revision": result["task"]["revision"],
                "annotation": fixtures.annotation("INDIVIDUAL", ["1"]),
            }, self.worker_csrf)
            try:
                self.assertTrue(entered.wait(3))
                status, data = self.request(self.admin, "/api/admin/users/update", {
                    "user_id": uid, "active": False,
                }, self.admin_csrf)
                self.assertEqual(status, 200, data)
            finally:
                release.set()
            status, data = future.result(timeout=5)
        self.assertEqual(status, 401, data)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM annotations").fetchone()[0], 0)

    def test_non_ascii_csrf_is_forbidden_instead_of_server_error(self):
        status, _ = self.request(self.admin, "/api/admin/import", {"text": "[]"}, "é")
        self.assertEqual(status, 403)


class AuditDataTests(unittest.TestCase):
    def test_invalid_identity_and_subject_types_are_rejected(self):
        for identity in (None, True, {}, []):
            with self.subTest(identity=identity):
                row = fixtures.sample("invalid")
                row["query_boxes"][0]["identity_id"] = identity
                row["target_boxes"][0]["identity_id"] = identity
                with self.assertRaises(InputError):
                    normalize_task(row)
        value = fixtures.annotation("INDIVIDUAL", ["1"])
        value["subjects"][0]["subject_id"] = True
        with self.assertRaises(InputError):
            normalize_annotation(value, ["1"], True)
        value = fixtures.annotation("INDIVIDUAL", ["1"])
        value["select_texts"] = ["bad \ud800"]
        with self.assertRaises(InputError):
            normalize_annotation(value, ["1"], True)

    def test_converter_preserves_provenance_for_completed_annotations(self):
        source = fixtures.sample("old")
        source.update(normalize_annotation(fixtures.annotation("INDIVIDUAL", ["1"]), ["1"], True))
        source["annotator_email"] = "Author@Example.COM"
        pairs = {
            "images": [{"image_id": "q", "url": "train/q.png"}, {"image_id": "t", "url": "train/t.png"}],
            "boxes": [{"box_id": side, "label": "1", "x": .1, "y": .1, "width": .2, "height": .3}
                      for side in ("q", "t")],
            "pairs": [{"pair_id": "pair", "query_image_id": "q", "target_image_id": "t", "split": "TRAIN"}],
            "pair_links": [{"pair_id": "pair", "box_id": "q", "side": "QUERY"},
                           {"pair_id": "pair", "box_id": "t", "side": "TARGET"}],
        }
        rows, skipped = build_rows(pairs, [source], False, "submitted")
        self.assertEqual(skipped, {})
        self.assertEqual(rows[0].get("annotator_email"), "Author@example.com")
        rows, skipped = build_rows(pairs, [source], False, "unassigned")
        self.assertNotIn("annotator_email", rows[0])

    def test_gemini_25_uses_budget_and_3_uses_level(self):
        answer = {"select_texts": ["the man"], "target_condition": "Subject 1 is seated"}
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}
        for model, expected in (("gemini-2.5-flash", {"thinkingBudget": 1024}),
                                ("gemini-3-flash-preview", {"thinkingLevel": "low"})):
            with self.subTest(model=model), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as call:
                gemini_suggestion(model, "test", "INDIVIDUAL", [{"subject_id": 1, "identity_ids": ["1"]}],
                                  ["the man"], "Subject 1 is seated", "")
                self.assertEqual(json.loads(call.call_args.args[0].data)["generationConfig"]["thinkingConfig"], expected)
