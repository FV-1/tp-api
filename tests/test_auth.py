import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import jwt
from fastapi.testclient import TestClient

from app import main


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "SANDBOXD_OPERATOR_KEY": "test-operator-key-that-is-at-least-32-bytes",
            "SANDBOXD_JWT_SECRET": "test-jwt-secret-that-is-at-least-32-bytes",
            "SANDBOXD_AUDIT_LOG": os.path.join(self.temp_dir.name, "audit.jsonl"),
        })
        self.environment.start()
        self.client = TestClient(main.app)
        main.SANDBOXES.clear()
        main.SANDBOXES["sbx-aaaaaaaa"] = {
            "id": "sbx-aaaaaaaa",
            "image": "alpine:3.20",
            "status": "ready",
            "ttl_seconds": 600,
            "limits": {"cpus": 1, "memory_mb": 512},
            "created_at": datetime.now(timezone.utc),
            "version": 1,
        }
        main.SANDBOXES["sbx-bbbbbbbb"] = {
            **main.SANDBOXES["sbx-aaaaaaaa"],
            "id": "sbx-bbbbbbbb",
        }

    def tearDown(self):
        self.client.close()
        main.SANDBOXES.clear()
        self.environment.stop()
        self.temp_dir.cleanup()

    def issue_agent_token(self, sandbox_id="sbx-aaaaaaaa"):
        response = self.client.post(
            f"/v1/sandboxes/{sandbox_id}/tokens",
            headers={"X-Operator-Key": os.environ["SANDBOXD_OPERATOR_KEY"]},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["expires_in"], 900)
        token = response.json()["access_token"]
        claims = jwt.decode(
            token,
            os.environ["SANDBOXD_JWT_SECRET"],
            algorithms=["HS256"],
            audience="sandboxd",
            issuer="sandboxd",
        )
        self.assertEqual(claims["sub"], sandbox_id)
        self.assertEqual(claims["exp"] - claims["iat"], 900)
        return token

    def test_missing_authentication_is_401_and_audited(self):
        response = self.client.get("/v1/sandboxes/sbx-aaaaaaaa")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["content-type"], "application/problem+json")

        with open(os.environ["SANDBOXD_AUDIT_LOG"], encoding="utf-8") as audit_file:
            event = json.loads(audit_file.readline())
        self.assertEqual(event["actor"], "anonymous")
        self.assertEqual(event["status"], 401)

    def test_agent_can_read_own_sandbox_but_not_use_operator_actions(self):
        token = self.issue_agent_token()
        headers = {"Authorization": f"Bearer {token}"}

        own = self.client.get("/v1/sandboxes/sbx-aaaaaaaa", headers=headers)
        self.assertEqual(own.status_code, 200)

        operator_action = self.client.get("/v1/sandboxes", headers=headers)
        self.assertEqual(operator_action.status_code, 403)

        with open(os.environ["SANDBOXD_AUDIT_LOG"], encoding="utf-8") as audit_file:
            events = [json.loads(line) for line in audit_file]
        self.assertTrue(any(event["actor"] == "sbx-aaaaaaaa" and event["status"] == 200 for event in events))

    def test_agent_cannot_learn_about_another_sandbox(self):
        token = self.issue_agent_token()
        response = self.client.get(
            "/v1/sandboxes/sbx-bbbbbbbb",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_operator_key_is_401(self):
        response = self.client.get(
            "/v1/sandboxes",
            headers={"X-Operator-Key": "wrong-key"},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
