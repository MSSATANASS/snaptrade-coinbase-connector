import json
import os
import tempfile
import unittest


class ClientAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name

        import importlib
        self.m = importlib.import_module("app")

        with self.m.users_lock:
            users = {
                "u1": {
                    "user_secret": "s1",
                    "created_at": self.m.now_iso(),
                    "connections": [],
                    "last_connected": None,
                }
            }
            self.m.save_users(users)

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_login_token_is_hashed_at_rest(self):
        token = self.m.issue_client_login_token("u1", ttl_days=1)
        path = self.m.resolve_data_path()
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(token, raw)
        data = json.loads(raw)
        auth = data["u1"].get("client_auth") or {}
        self.assertEqual(auth.get("login_token_hash"), self.m.sha256_hex(token))
        self.assertIn("login_token_expires_at", auth)

    def test_login_token_maps_to_user(self):
        token = self.m.issue_client_login_token("u1", ttl_days=1)
        uid = self.m.find_user_id_by_login_token(token)
        self.assertEqual(uid, "u1")

    def test_session_token_maps_to_user(self):
        session = self.m.issue_session_for_user("u1", ttl_hours=1)
        uid = self.m.find_user_id_by_session_token(session)
        self.assertEqual(uid, "u1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
