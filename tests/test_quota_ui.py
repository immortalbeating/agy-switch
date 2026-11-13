"""Render the real frontend with isolated account fixtures and virtual input."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

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
            color_scheme=theme, reduced_motion="reduce", timezone_id="Asia/Shanghai")
        self.contexts.append(context)
        context.add_init_script(
            "localStorage.setItem('agy.theme', " + json.dumps(theme) + ");"
            "localStorage.setItem('agy.lang', " + json.dumps(lang) + ");")
        account = {"id": "fixture-account", "label": "示例账号", "email": "demo@example.invalid",
                   "current": True, "notch": "#4c8bd9", "plan_type": "Pro",
                   "quota": {"tier": "Pro", "fetched_at": datetime.now(timezone.utc).isoformat(),
                             "groups": copy.deepcopy(groups), "error": error}}
        account["quota"].update(quota_extra or {})
        payload = {"accounts": [account], "current_id": "fixture-account", "live_email": None,
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
                account_id = parse_qs(urlsplit(request.request.url).query).get("id", ["fixture-account"])[0]
                request.fulfill(json=next(item for item in payload["accounts"] if item["id"] == account_id))
            elif path == "/api/switch":
                account_id = request.request.post_data_json["id"]
                payload["current_id"] = account_id
                for item in payload["accounts"]:
                    item["current"] = item["id"] == account_id
                request.fulfill(json={"ok": True, "restart_needed": False})
            elif path in ("/api/rename", "/api/export", "/api/delete"):
                data = request.request.post_data_json
                if path == "/api/rename":
                    next(item for item in payload["accounts"] if item["id"] == data["id"])["label"] = data["label"]
                elif path == "/api/delete":
                    payload["accounts"] = [item for item in payload["accounts"] if item["id"] != data["id"]]
                request.fulfill(json={"ok": True, "content": "{}"})
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
        page.locator(".acc-refresh").click()
        page.wait_for_function("document.querySelectorAll('.quota-table tbody tr').length === 3")
        self.assertEqual(page.locator(".quota-table tbody .qtitle").all_text_contents(),
                         ["Gemini", "Gemini 4 Argon", "Claude / GPT"])

    def test_switch_moves_the_current_account_to_the_top_in_stable_order(self):
        page, payload = self.open_ui(three_groups())
        template = payload["accounts"][0]
        labels = ["账号甲", "账号乙", "账号丙", "账号丁", "账号戊"]
        payload["accounts"] = [dict(copy.deepcopy(template), id=str(i), label=label, current=i == 2)
                               for i, label in enumerate(labels)]
        payload["current_id"] = "2"
        page.reload()
        names = page.locator("#list .acc-name")
        names.first.wait_for()
        self.assertEqual(names.all_text_contents(), [labels[i] for i in (2, 0, 1, 3, 4)])
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("#list .acc").filter(has_text=labels[4]).locator(".btn-switch").click()
        page.locator("#list .acc.current .acc-name").filter(has_text=labels[4]).wait_for()
        self.assertEqual(names.all_text_contents(), [labels[i] for i in (4, 0, 1, 2, 3)])
        self.assertEqual(page.evaluate("state.data.accounts.map(a => a.id)"), [str(i) for i in range(5)])
        self.assertLess(page.evaluate("window.scrollY"), 1)


    def test_compact_cards_preserve_expansion_across_poll_and_switch(self):
        page, payload = self.open_ui(three_groups())
        template = payload["accounts"][0]
        payload["accounts"] = [dict(copy.deepcopy(template), id=str(i), label=f"Account {i}", current=i == 1)
                               for i in range(3)]
        payload["current_id"] = "1"
        page.reload()
        cards = page.locator("#list .acc")
        self.assertEqual(cards.evaluate_all("es => es.map(e => e.dataset.accountId)"), ["1", "0", "2"])
        self.assertEqual(cards.evaluate_all("es => es.map(e => e.dataset.expanded)"), ["true", "false", "false"])
        current = page.locator('.acc[data-account-id="1"]')
        other = page.locator('.acc[data-account-id="0"]')
        current.locator('.acc-expand').click()
        other.locator('.acc-expand').click()
        page.evaluate("loadState()")
        page.wait_for_function("document.querySelector('[data-account-id=\"0\"]').dataset.expanded === 'true'")
        self.assertEqual(current.get_attribute("data-expanded"), "false")
        self.assertTrue(other.locator('.acc-details').is_visible())
        self.assertTrue(current.locator('.acc-details').is_hidden())
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator('.acc[data-account-id="2"] .btn-switch').click()
        page.wait_for_function("document.querySelector('#list .acc').dataset.accountId === '2'")
        self.assertEqual(page.locator('#list .acc').first.get_attribute('data-expanded'), 'true')
        self.assertEqual(page.locator('.acc[data-account-id="1"]').get_attribute('data-expanded'), 'false')
        self.assertEqual(page.locator('.acc[data-account-id="0"]').get_attribute('data-expanded'), 'true')
        self.assertEqual(page.evaluate("state.data.accounts.map(a => a.id)"), ["0", "1", "2"])

    def test_collapsed_summary_uses_real_pools_and_expands(self):
        groups = [{"name": "Carbon", "buckets": [bucket("5h", 17)]},
                  {"name": "Unknown Pool", "buckets": [bucket("week", 31)]},
                  {"name": "Gemini 4 Argon", "buckets": [bucket("week", 68)]},
                  {"name": "Extra Pool", "buckets": [bucket("week", 42)]}]
        page, _ = self.open_ui(groups)
        card = page.locator('.acc')
        card.locator('.acc-expand').click()
        items = card.locator('.quota-summary > .quota-summary-item')
        self.assertEqual(items.count(), 3)
        for item, name, pct in zip(items.all(), ['Carbon', 'Unknown Pool', 'Argon'], ['17%', '31%', '68%']):
            self.assertIn(name, item.inner_text())
            self.assertIn(pct, item.inner_text())
        more = card.locator('.quota-more')
        self.assertIn('+1', more.inner_text())
        self.assertTrue(card.locator('.acc-details').is_hidden())
        more.click()
        self.assertTrue(card.locator('.acc-details').is_visible())
        self.assertEqual(card.locator('.quota-table tbody tr').first.locator('.qmissing').count(), 1)

    def test_menu_actions_closure_and_poll_focus(self):
        page, payload = self.open_ui(shared_groups())
        card = page.locator('.acc')
        toggle = card.locator('.acc-menu-toggle')
        toggle.click()
        menu = card.locator('.action-menu')
        self.assertTrue(menu.is_visible())
        self.assertEqual(menu.locator('.menu-item').count(), 4)
        menu.locator('[data-action="refresh"]').focus()
        page.evaluate('loadState()')
        page.wait_for_function("document.activeElement?.dataset.focusKey === 'refresh-fixture-account'")
        page.keyboard.press('Escape')
        self.assertTrue(menu.is_hidden())
        self.assertEqual(page.evaluate('document.activeElement.dataset.focusKey'), 'account-menu-fixture-account-toggle')
        toggle.click()
        page.locator('.acc-name').click()
        self.assertTrue(menu.is_hidden())
        toggle.click()
        with page.expect_response(lambda response: '/api/quota?' in response.url):
            menu.locator('[data-action="refresh"]').click()
        self.assertTrue(menu.is_hidden())
        page.on('dialog', lambda dialog: dialog.accept('Renamed' if dialog.type == 'prompt' else None))
        toggle.click()
        menu.locator('[data-action="rename"]').click()
        page.wait_for_function("state.data.accounts[0].label === 'Renamed'")
        with page.expect_download() as download:
            toggle.click()
            menu.locator('[data-action="export"]').click()
        self.assertTrue(download.value.suggested_filename.endswith('.json'))
        toggle.click()
        menu.locator('[data-action="delete"]').click()
        page.wait_for_function("state.data.accounts.length === 0")
        self.assertEqual(payload['accounts'], [])

    def test_add_and_import_dialog_cancel_restores_menu_focus(self):
        for action in ('#btnAdd', '#btnImport'):
            with self.subTest(action=action):
                page, _ = self.open_ui(shared_groups())
                page.locator('#btnAddMenu').click()
                page.locator(action).click()
                dialog = page.locator('#dlgAdd' if action == '#btnAdd' else '#dlgImport')
                self.assertTrue(dialog.is_visible())
                page.keyboard.press('Escape')
                self.assertEqual(page.evaluate('document.activeElement.id'), 'btnAddMenu')

    def test_demo_screenshots(self):
        output = Path(__file__).resolve().parents[1] / '.ref' / 'compact-cards-v1.2.0'
        output.mkdir(parents=True, exist_ok=True)
        groups = [{"name": "Carbon", "buckets": [bucket("5h", 17)]},
                  {"name": "Unknown Pool", "buckets": [bucket("week", 31)]},
                  {"name": "Gemini 4 Argon", "buckets": [bucket("week", 68)]},
                  {"name": "Extra Pool", "buckets": [bucket("week", 42)]}]
        for width in (320, 360, 520):
            for theme in ('light', 'dark'):
                page, payload = self.open_ui(groups, width=width, theme=theme)
                template = payload['accounts'][0]
                payload['accounts'] = [dict(copy.deepcopy(template), id=str(i), label=f'演示账号 {i + 1}',
                                            email=f'demo{i + 1}@example.invalid', current=i == 1) for i in range(3)]
                payload['current_id'] = '1'
                page.reload()
                page.locator('#list .acc').first.wait_for()
                page.screenshot(path=str(output / f'demo-{width}-{theme}.png'), full_page=True)
        (output / 'metadata.json').write_text(json.dumps({"fixtures": "synthetic example.invalid accounts; no real authentication", "widths": [320, 360, 520], "themes": ["light", "dark"], "browser": "headless Chromium --enable-gpu"}, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_email_as_display_name_is_not_duplicated(self):
        page, payload = self.open_ui(shared_groups(), width=320)
        email = 'a-very-long-account-address-that-needs-ellipsis@example.invalid'
        payload['accounts'][0].update(label=email, email=email)
        page.reload()
        card = page.locator('.acc')
        self.assertIn(email, card.locator('.acc-name').get_attribute('title'))
        self.assertEqual(card.locator('.acc-email').count(), 0)
        self.assertEqual(card.inner_text().count(email), 1)

    def test_sync_time_has_room_and_full_detail_with_pointer_and_keyboard(self):
        for width, theme, lang in [(320, "light", "zh"), (360, "dark", "en")]:
            with self.subTest(width=width, theme=theme, lang=lang):
                page, payload = self.open_ui(shared_groups(), width=width, theme=theme, lang=lang,
                    quota_extra={"fetched_at": "2026-11-03T08:30:45Z"})
                email = "a-very-long-account-address-that-needs-ellipsis@example.invalid"
                payload["accounts"][0]["email"] = email
                page.reload()
                address = page.locator(".acc-email")
                address.wait_for()
                self.assertEqual(address.get_attribute("title"), email)
                self.assertTrue(address.evaluate("e => e.scrollWidth > e.clientWidth"))
                sync = page.locator(".acc-sync")
                detail = sync.get_by_role("tooltip", include_hidden=True)
                self.assertIn("11-03 16:30", sync.locator("time").inner_text())
                self.assertTrue(sync.evaluate("""e => {
                    const time = e.querySelector('time').getBoundingClientRect();
                    const actions = e.parentElement.querySelector('.acc-refresh').getBoundingClientRect();
                    return time.right < actions.left;
                }"""))
                self.assertTrue(detail.is_hidden())
                sync.hover()
                self.assertTrue(detail.is_visible())
                self.assertIn("2026-11-03 16:30:45", detail.inner_text())
                detail.hover()
                self.assertTrue(detail.is_visible())
                page.keyboard.press("Escape")
                self.assertTrue(detail.is_hidden())
                page.locator(".acc-name").hover()
                sync.focus()
                self.assertTrue(sync.evaluate("e => document.activeElement === e"))
                self.assertTrue(detail.is_visible())
                page.keyboard.press("Escape")
                self.assertTrue(detail.is_hidden())

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
                page.locator(".acc-refresh").click()
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
