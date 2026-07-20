import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).parents[1] / "tools/shotgrid_backup"
sys.path.insert(0, str(TOOL_DIR))
import app  # noqa: E402
from backup import safe_error  # noqa: E402


class AppTests(unittest.TestCase):
    def payload(self, output: str, **updates):
        value = {
            "site_url": "https://example.shotgrid.autodesk.com",
            "script_name": "backup",
            "script_key": "test-secret-value",
            "http_proxy": "127.0.0.1:7892",
            "output": output,
        }
        value.update(updates)
        return value

    def test_settings_are_full_backup_only(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = app.validate_settings(self.payload(temp))
        self.assertTrue(settings["download_attachments"])
        self.assertFalse(settings["copy_external"])
        self.assertIsNone(settings["updated_since"])
        self.assertGreaterEqual(settings["workers"], 1)

    def test_completion_message_respects_optional_external_copy(self):
        state = app.JobState()
        state.begin({}, "media_supplement", copy_external=False)
        state.finish("/tmp/supplement")
        self.assertEqual(state.status, "complete")
        self.assertIn("ShotGrid 托管媒体已补全", state.message)
        self.assertIn("外部文件未请求复制", state.message)

        state.begin({}, "media_supplement", copy_external=True)
        state.finish("/tmp/supplement")
        self.assertIn("外部媒体已补全", state.message)

    def test_rejects_site_credentials_and_authenticated_proxy(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                app.validate_settings(self.payload(temp, site_url="https://user:pass@example.com"))
            with self.assertRaises(ValueError):
                app.validate_settings(self.payload(temp, http_proxy="user:pass@127.0.0.1:7892"))

    def test_credential_handle_is_single_use(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = app.validate_settings(self.payload(temp))
            handle = app.store_credential(settings)
            no_key = app.validate_settings(self.payload(temp, script_key=""), require_key=False)
            payload = {"credential_handle": handle}
            self.assertEqual(app.consume_credential(payload, no_key), "test-secret-value")
            with self.assertRaises(RuntimeError):
                app.consume_credential(payload, no_key)

    def test_safe_error_redacts_secret_and_proxy_userinfo(self):
        secret = "test-secret-value"
        result = safe_error(
            RuntimeError(f"api_key={secret} https://user:pass@proxy.invalid token=abc"),
            [secret],
        )
        self.assertNotIn(secret, result["message"])
        self.assertNotIn("user:pass", result["message"])
        self.assertNotIn("token=abc", result["message"])


if __name__ == "__main__":
    unittest.main()
