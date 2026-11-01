"""Offline API error regression tests, isolated from private configuration."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError


class ApiErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location("agy_api_test", Path(__file__).resolve().parents[1] / "agy_switch.py")
        cls.app = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"USERPROFILE": cls.home.name, "HOME": cls.home.name, "AGY_OAUTH_CONFIG": ""}), mock.patch.object(Path, "home", return_value=Path(cls.home.name)):
            spec.loader.exec_module(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def http_error(self, status, reason="", url="", message="denied", raw=None):
        data = {"error": {"message": message, "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "metadata": {"validation_url": url}}]}}
        return HTTPError("https://example.test", status, "error", {}, io.BytesIO(raw if raw is not None else json.dumps(data).encode()))

    def test_validation_details_and_no_host_fallback(self):
        url = "https://accounts.google.com/v3/signin/continue?token=test"
        with mock.patch.object(self.app.urllib.request, "urlopen", side_effect=self.http_error(403, "VALIDATION_REQUIRED", url, "Verify account")) as request:
            with self.assertRaises(self.app.ApiAuthError) as raised:
                self.app.api_post("fake", "retrieveUserQuotaSummary", {})
        self.assertEqual(request.call_count, 1)
        err = raised.exception
        self.assertEqual((err.status, err.reason, err.google_message, err.validation_url), (403, "VALIDATION_REQUIRED", "Verify account", url))

    def test_invalid_urls_and_non_json(self):
        for url in ("http://accounts.google.com/", "https://accounts.google.com.evil.test/", "https://user@accounts.google.com/", "https://user:pass@accounts.google.com/", "https://accounts.google.com:444/", "https://accounts.google.com:bad/"):
            with self.subTest(url=url):
                self.assertEqual(self.app._parse_auth_error(self.http_error(403, "VALIDATION_REQUIRED", url)).validation_url, "")
        err = self.app._parse_auth_error(self.http_error(403, raw=b"not json"))
        self.assertEqual((err.status, err.reason, err.google_message, err.validation_url), (403, "", "", ""))
        self.assertEqual(self.app._safe_validation_url("https://accounts.google.com:443/path"), "https://accounts.google.com:443/path")

    def test_quota_auth_failures_are_not_swallowed(self):
        for method in ("loadCodeAssist", "fetchAvailableModels", "retrieveUserQuotaSummary"):
            for status, reason in ((401, ""), (401, "VALIDATION_REQUIRED"), (403, "VALIDATION_REQUIRED"), (403, "PERMISSION_DENIED")):
                with self.subTest(method=method, status=status, reason=reason):
                    url = "https://accounts.google.com/v3/signin/continue?x=1"
                    def post(token, called, body):
                        if called == method:
                            raise self.app._parse_auth_error(self.http_error(status, reason, url, "Google detail"))
                        if called == "loadCodeAssist":
                            return {"paidTier": {"name": "Pro"}, "currentTier": {"displayName": "Free"}}
                        if called == "fetchAvailableModels":
                            return {"models": {"gemini-test": {"quotaInfo": {"remainingFraction": 0.5}}}}
                        return {}
                    with mock.patch.object(self.app, "ensure_token", return_value="fake"), mock.patch.object(self.app, "api_post", side_effect=post):
                        quota = self.app.refresh_quota({"email": "test@example.test", "creds": {}})
                    self.assertEqual(quota["models"], [])
                    self.assertEqual(quota["http_status"], status)
                    self.assertEqual(quota["error_code"], reason or f"HTTP_{status}")
                    self.assertEqual(quota["google_message"], "Google detail")
                    self.assertEqual(quota["validation_url"], url if status == 403 and reason == "VALIDATION_REQUIRED" else "")
                    self.assertEqual(quota["tier"], "" if method == "loadCodeAssist" else "Pro")
                    self.assertNotIn("gemini-cli", quota["error"])
                    if status == 401:
                        self.assertIn("认证", quota["error"])
                    elif reason == "VALIDATION_REQUIRED":
                        self.assertIn("验证", quota["error"])

    def test_malformed_details_and_status_fallback(self):
        cases = [
            ({"message": ["bad"], "status": "PERMISSION_DENIED", "details": [{"@type": "other", "reason": "VALIDATION_REQUIRED", "metadata": {"validation_url": "https://accounts.google.com/"}}]}, "PERMISSION_DENIED", ""),
            ({"message": {"bad": 1}, "status": 123, "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": ["bad"]}]}, "", ""),
            ({"message": "detail", "status": "UNAUTHENTICATED", "details": None}, "UNAUTHENTICATED", "detail"),
            ({"message": "detail", "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "VALIDATION_REQUIRED", "metadata": []}]}, "VALIDATION_REQUIRED", "detail"),
        ]
        for detail, reason, message in cases:
            with self.subTest(detail=detail):
                raw = json.dumps({"error": detail}).encode()
                err = self.app._parse_auth_error(self.http_error(403, raw=raw))
                self.assertEqual((err.reason, err.google_message, err.validation_url), (reason, message, ""))

    def test_refresh_to_account_view_preserves_safe_diagnostics_without_credentials(self):
        url = "https://accounts.google.com/v3/signin/continue?x=1"
        acc = {"id": "test", "email": "test@example.test", "creds": {"access_token": "private-token"}, "private": "private-value"}
        def post(token, method, body):
            if method == "loadCodeAssist":
                return {"paidTier": {"name": "Pro"}}
            if method == "fetchAvailableModels":
                return {"models": {}}
            raise self.app._parse_auth_error(self.http_error(403, "VALIDATION_REQUIRED", url))
        with mock.patch.object(self.app, "ensure_token", return_value="fake"), mock.patch.object(self.app, "api_post", side_effect=post):
            self.app.refresh_quota(acc)
        view = self.app.account_view(acc, "test")
        self.assertEqual((view["quota"]["error_code"], view["quota"]["http_status"], view["quota"]["validation_url"]), ("VALIDATION_REQUIRED", 403, url))
        self.assertEqual(view["quota"]["tier"], "Pro")
        self.assertNotIn("creds", view)
        self.assertNotIn("private", view)
        self.assertNotIn("private-token", json.dumps(view))
        for status, code, candidate in ((403, "PERMISSION_DENIED", url), (401, "VALIDATION_REQUIRED", url), (403, "VALIDATION_REQUIRED", "https://evil.test/")):
            acc["quota"].update(http_status=status, error_code=code, validation_url=candidate)
            self.assertEqual(self.app.account_view(acc, "test")["quota"]["validation_url"], "")
        acc["quota"] = {}
        quota = self.app.account_view(acc, "test")["quota"]
        self.assertEqual((quota["error_code"], quota["http_status"], quota["validation_url"]), ("", None, ""))

    def test_non_auth_errors_still_degrade(self):
        def post(token, method, body):
            if method == "fetchAvailableModels":
                return {"models": {"gemini-test": {"quotaInfo": {"remainingFraction": 0.5}}}}
            raise RuntimeError("offline")
        with mock.patch.object(self.app, "ensure_token", return_value="fake"), mock.patch.object(self.app, "api_post", side_effect=post):
            quota = self.app.refresh_quota({"email": "test@example.test", "creds": {}})
        self.assertEqual(quota["models"][0]["pct"], 50)
        self.assertEqual(quota["error"], "")


if __name__ == "__main__":
    unittest.main()
