import ast
import base64
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


class PublishConflictTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).parents[1] / "publish.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "publish_snapshot")
        self.requests = Mock()
        namespace = dict(requests=self.requests, base64=base64, json=json, dt=dt, API="test")
        exec(compile(ast.Module(body=[function], type_ignores=[]), "publish.py", "exec"), namespace)
        self.publish = namespace["publish_snapshot"]
        self.payload = dict(snapshot_date="2026-11-17", generated_at_utc="2026-11-17T18:13:00Z")

    def existing(self, sha, timestamp="2026-11-17T18:00:00Z"):
        response = Mock()
        content = base64.b64encode(json.dumps(dict(generated_at_utc=timestamp)).encode()).decode()
        response.json.return_value = dict(sha=sha, content=content)
        return response

    def response(self, code):
        response = Mock(status_code=code)
        if code >= 400:
            response.raise_for_status.side_effect = RuntimeError(str(code))
        return response

    def test_conflict_refreshes_revision_before_retry(self):
        self.requests.get.side_effect = [self.existing("old"), self.existing("new")]
        self.requests.put.side_effect = [self.response(409), self.response(200)]
        self.assertTrue(self.publish(self.payload, {}))
        self.assertEqual([c.kwargs["json"]["sha"] for c in self.requests.put.call_args_list],
                         ["old", "new"])
        self.assertTrue(all(c.kwargs["params"] == {"ref": "main"}
                            for c in self.requests.get.call_args_list))

    def test_repeated_conflicts_stop_after_three_attempts(self):
        self.requests.get.return_value = self.existing("old")
        self.requests.put.return_value = self.response(409)
        with self.assertRaisesRegex(RuntimeError, "409"):
            self.publish(self.payload, {})
        self.assertEqual(self.requests.put.call_count, 3)

    def test_other_errors_are_not_retried(self):
        self.requests.get.return_value = self.existing("old")
        self.requests.put.return_value = self.response(403)
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.publish(self.payload, {})
        self.assertEqual(self.requests.put.call_count, 1)

    def test_newer_snapshot_is_preserved(self):
        self.requests.get.return_value = self.existing("new", "2026-11-17T18:14:00Z")
        self.assertFalse(self.publish(self.payload, {}))
        self.requests.put.assert_not_called()
