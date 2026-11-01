"""Render the real frontend with isolated account fixtures and virtual input."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def bucket(window, pct):
    return {"window": window, "pct": pct,
            "reset": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}


def shared_groups():
    return [
        {"name": "Gemini Models", "buckets": [bucket("week", 99.8), bucket("5h", 100)]},
        {"name": "Claude and GPT models", "buckets": [bucket("week", 100), bucket("5h", 42)]},
    ]


def three_groups():
    groups = shared_groups()
    groups.insert(1, {"name": "Gemini 4 Argon", "buckets": [bucket("week", 68.4)]})
    return groups


class QuotaUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = tempfile.TemporaryDirectory(prefix="agy-quota-ui-")
        cls.html = (Path(__file__).resolve().parents[1] / "web/index.html").read_text(encoding="utf-8")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, args=["--enable-gpu"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.artifacts.cleanup()

    def setUp(self):
        self.contexts = []

    def tearDown(self):
        for context in self.contexts:
            context.close()

    def open_ui(self, groups, width=360, theme="light", lang="zh", error="", quota_extra=None):
        context = self.browser.new_context(
            viewport={"width": width, "height": 850}, device_scale_factor=2,
            color_scheme=theme, reduced_motion="reduce")
        self.contexts.append(context)
        context.add_init_script(
            "localStorage.setItem('agy.theme', " + json.dumps(theme) + ");"
            "localStorage.setItem('agy.lang', " + json.dumps(lang) + ");")
        account = {"id": "fixture-account", "label": "示例账号", "email": "demo@example.invalid",
                   "current": False, "notch": "#4c8bd9", "plan_type": "Pro",
                   "quota": {"tier": "Pro", "fetched_at": datetime.now(timezone.utc).isoformat(),
                             "groups": copy.deepcopy(groups), "error": error}}
        account["quota"].update(quota_extra or {})
        payload = {"accounts": [account], "current_id": None, "live_email": None,
                   "ide_email": None, "creds_email": None, "ide_running": False,
                   "ide_path": "", "creds_path": ""}

        def route(request):
            path = urlsplit(request.request.url).path
            if path == "/":
                request.fulfill(status=200, content_type="text/html", body=self.html)
            elif path == "/api/state":
                request.fulfill(json=payload)
            elif path == "/api/prefs":
                request.fulfill(json={"theme": theme, "lang": lang, "close_to_tray": True})
            elif path == "/api/quota":
                request.fulfill(json=payload["accounts"][0])
            else:
                request.fulfill(status=404, json={"error": "Unexpected fixture request"})

        context.route("**/*", route)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://agy-switch.test/", wait_until="load")
        page.locator(".acc").wait_for()
        self.assertEqual(errors, [])
        return page, payload

    def test_shared_pools_render_as_rows_and_time_windows_as_columns(self):
        page, _ = self.open_ui(shared_groups())
        self.assertEqual(page.locator(".quota-table thead th").all_text_contents(), ["模型组", "5小时", "本周"])
        self.assertEqual(page.locator(".quota-table tbody .qtitle").all_text_contents(), ["Gemini", "Claude / GPT"])
        self.assertEqual(page.locator(".quota-table tbody tr").count(), 2)

    def test_weekly_only_argon_is_an_ordinary_row_with_a_blank_five_hour_cell(self):
        page, _ = self.open_ui(three_groups())
        argon = page.locator(".quota-table tbody tr").nth(1)
        self.assertEqual(argon.locator("th").inner_text(), "Gemini 4 Argon")
        missing = argon.locator("td").nth(0)
        self.assertEqual(missing.inner_text(), "")
        self.assertEqual(missing.locator("*").count(), 0)
        self.assertEqual(argon.locator("td").nth(1).locator(".qpct").inner_text(), "68.4%")
        visible = page.locator(".quota-grid").inner_text()
        for text in ("未返回", "独立周额度", "无此时段额度"):
            self.assertNotIn(text, visible)
        self.assertEqual(argon.locator('.qsummary').count(), 1)
        self.assertEqual(argon.locator('.qreset').count(), 1)

    def test_real_zero_stays_empty_and_near_full_is_not_rounded_to_full(self):
        groups = [{"name": "Gemini Models", "buckets": [bucket("5h", 0), bucket("week", 99.999)]}]
        page, _ = self.open_ui(groups)
        self.assertEqual(page.locator(".qpct").all_text_contents(), ["0%", "99.9%"])
        self.assertEqual(page.locator(".qfill").first.evaluate("e => e.style.width"), "0%")
        self.assertEqual(page.locator("[role=progressbar]").first.get_attribute("aria-valuenow"), "0")

    def test_only_reported_windows_are_present_and_unknown_names_are_preserved(self):
        name = "Gemini Future <preview>"
        page, _ = self.open_ui([{"name": name, "buckets": [bucket("weekly", 72.5)]}], lang="en")
        self.assertEqual(page.locator(".quota-table thead th").all_text_contents(), ["Model group", "Weekly"])
        self.assertEqual(page.locator(".qtitle").inner_text(), name)
        self.assertEqual(page.locator(".qtitle preview").count(), 0)
        self.assertEqual(page.locator(".qmissing").count(), 0)

    def test_empty_and_error_states_do_not_invent_full_or_empty_quota_values(self):
        page, _ = self.open_ui([{"name": "Unknown", "buckets": [bucket("week", None)]}])
        self.assertEqual(page.locator(".q-empty").count(), 1)
        self.assertEqual(page.locator(".qbar").count(), 0)
        page, _ = self.open_ui([], error="<quota unavailable>")
        self.assertIn("<quota unavailable>", page.locator(".q-error").inner_text())
        self.assertEqual(page.locator(".q-error quota").count(), 0)

    def test_refresh_can_add_a_third_group_without_a_second_gemini_label(self):
        page, payload = self.open_ui(shared_groups())
        payload["accounts"][0]["quota"]["groups"] = three_groups()
        page.locator(".acc-ghost-actions button").first.click()
        page.wait_for_function("document.querySelectorAll('.quota-table tbody tr').length === 3")
        self.assertEqual(page.locator(".quota-table tbody .qtitle").all_text_contents(),
                         ["Gemini", "Gemini 4 Argon", "Claude / GPT"])

    def test_verification_opens_only_the_official_link_and_refresh_can_recover(self):
        url = "https://accounts.google.com/v3/signin/continue?fixture=1&continue=https%3A%2F%2Fdevelopers.google.com%2F"
        for theme, lang in [("light", "zh"), ("dark", "en")]:
            with self.subTest(theme=theme, lang=lang):
                page, payload = self.open_ui([], theme=theme, lang=lang, error="verification needed", quota_extra={
                    "http_status": 403, "error_code": "VALIDATION_REQUIRED", "validation_url": url})
                # Intercept the native opener; no real browser or account is opened.
                page.evaluate("window.openedUrls = []; window.__TAURI__ = {opener:{openUrl: async url => window.openedUrls.push(url)}}")
                link = page.locator(".q-verify")
                self.assertIn("验证账号" if lang == "zh" else "Verify account", link.inner_text())
                link.click()
                self.assertEqual(page.evaluate("window.openedUrls"), [url])
                self.assertNotIn("gemini-cli", page.locator(".q-error").inner_text())
                self.assertEqual(page.locator(".qbar").count(), 0)
                self.assertFalse(page.locator(".q-error").evaluate("e => e.scrollWidth > e.clientWidth"))
                payload["accounts"][0]["quota"] = {"error": "", "groups": shared_groups()}
                page.locator(".acc-ghost-actions button").first.click()
                page.locator(".q-error").wait_for(state="hidden")
                self.assertEqual(page.locator(".q-error").count(), 0)
                self.assertEqual(page.locator(".qbar").count(), 4)

    def test_verification_without_a_safe_url_shows_manual_recovery(self):
        for url in ["", "javascript:alert(1)", "https://accounts.google.com.evil.invalid/",
                    "https://user@accounts.google.com/", "https://accounts.google.com:444/"]:
            with self.subTest(url=url):
                page, _ = self.open_ui([], error="verification needed", quota_extra={
                    "http_status": 403, "error_code": "VALIDATION_REQUIRED", "validation_url": url})
                self.assertEqual(page.locator(".q-verify").count(), 0)
                self.assertIn("官方 Antigravity", page.locator(".q-error").inner_text())

    def test_layout_in_both_themes_and_languages_at_small_window_sizes(self):
        for width, theme, lang in [(360, "light", "zh"), (410, "dark", "zh"), (360, "light", "en")]:
            with self.subTest(width=width, theme=theme, lang=lang):
                groups = three_groups()
                # Exercise both tomorrow's longest Chinese time and a later short date.
                groups[0]['buckets'][0]['reset'] = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
                groups[0]['buckets'][1]['reset'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                page, _ = self.open_ui(groups, width=width, theme=theme, lang=lang)
                overflow = page.locator(".quota-grid").evaluate("""root => ({
                    grid: root.scrollWidth > root.clientWidth + 1,
                    text: [...root.querySelectorAll('.qtitle,.qpct,.qreset')]
                        .filter(e => e.scrollWidth > e.clientWidth + 1).map(e => e.textContent)
                })""")
                self.assertEqual(overflow, {"grid": False, "text": []})
                geometry = page.locator(".quota-table td:not(.qmissing)").evaluate_all("""cells => cells.map(cell => {
                    const time = cell.querySelector('.qreset').getBoundingClientRect();
                    const pct = cell.querySelector('.qpct').getBoundingClientRect();
                    const bar = cell.querySelector('.qbar').getBoundingClientRect();
                    const bounds = cell.getBoundingClientRect();
                    return {sameLine: Math.abs((time.y + time.height / 2) - (pct.y + pct.height / 2)) < 1,
                        separate: time.right + 3 <= pct.left, inside: pct.right <= bounds.right + 1,
                        barBelow: bar.top >= Math.max(time.bottom, pct.bottom)};
                })""")
                for cell in geometry:
                    self.assertEqual(cell, {"sameLine": True, "separate": True, "inside": True, "barBelow": True})
                output = Path(self.artifacts.name)
                page.locator('.acc').screenshot(path=str(output / f"quota-inline-time-{width}-{theme}-{lang}.png"))


if __name__ == "__main__":
    unittest.main()
