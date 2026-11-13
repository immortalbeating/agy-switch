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
        self.order_requests = []
        self.order_failure = False

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
            elif path == "/api/accounts/order":
                ids = request.request.post_data_json["ids"]
                self.order_requests.append(ids)
                if self.order_failure:
                    request.fulfill(status=500, json={"error": "fixture save failed"})
                else:
                    live = [a["id"] for a in payload["accounts"]]
                    order = []
                    for account_id in ids + payload.get("account_order", []) + live:
                        if account_id in live and account_id not in order:
                            order.append(account_id)
                    payload["account_order"] = order
                    request.fulfill(json={"account_order": order})
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

    def prepare_order_accounts(self, page, payload, order=None):
        template = payload["accounts"][0]
        payload["accounts"] = [dict(copy.deepcopy(template), id=str(i), label=f"Account {i}",
                                   email=f"account{i}@example.invalid", current=i == 2)
                               for i in range(5)]
        payload["current_id"] = "2"
        if order is not None:
            payload["account_order"] = order
        page.reload()
        page.locator('.acc[data-account-id="4"]').wait_for()

    def card_order(self, page):
        return page.locator('#list .acc').evaluate_all('es => es.map(e => e.dataset.accountId)')

    def order_handle(self, page, account_id):
        return page.locator(f'.acc-row:has(> .acc[data-account-id="{account_id}"]) > .order-handle')

    def test_approved_card_style_and_stable_account_marks(self):
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                page, payload = self.open_ui(three_groups(), theme=theme)
                style = page.locator('.quota-table').evaluate('''table => ({
                    background: getComputedStyle(table.querySelector('thead th')).backgroundColor,
                    card: getComputedStyle(table.closest('.acc')).backgroundColor,
                    headerLine: getComputedStyle(table.querySelector('thead th')).borderBottomColor,
                    groupLine: getComputedStyle(table.querySelector('tbody tr + tr')).borderTopColor,
                    weekly: [...table.querySelectorAll('.qfill.week')].map(e => getComputedStyle(e).backgroundColor),
                    fiveHour: getComputedStyle(table.querySelector('.qfill:not(.week)')).backgroundColor
                })''')
                self.assertNotEqual(style['background'], style['card'])
                self.assertNotEqual(style['headerLine'], style['groupLine'])
                self.assertEqual(len(set(style['weekly'])), 1)
                self.assertNotEqual(style['weekly'][0], style['fiveHour'])
                self.prepare_order_accounts(page, payload)
                marks = page.locator('.acc-avatar').evaluate_all('es => es.map(e => e.innerHTML)')
                self.assertEqual(len(set(marks)), 5)
                self.assertEqual(page.locator('.acc-avatar').all_text_contents(), [''] * 5)
                mark = page.locator('[data-account-id="4"] .acc-avatar').inner_html()
                payload['accounts'][4]['label'] = 'Renamed account'
                page.evaluate('loadState()')
                page.get_by_text('Renamed account', exact=True).wait_for()
                self.assertEqual(page.locator('[data-account-id="4"] .acc-avatar').inner_html(), mark)

    def test_custom_order_survives_reload_and_switch_without_reordering_store(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload, ['3', '2', '0', '4', '1'])
        self.assertEqual(self.card_order(page), ['2', '3', '0', '4', '1'])
        card = page.locator('[data-account-id="1"]')
        card.locator('.acc-menu-toggle').click()
        card.locator('[data-action="move-up"]').click()
        page.wait_for_function("state.data.account_order.join() === '3,2,0,1,4'")
        self.assertEqual(self.card_order(page), ['2', '3', '0', '1', '4'])
        self.assertEqual(page.evaluate('document.activeElement.dataset.focusKey'), 'account-menu-1-toggle')
        page.reload()
        page.locator('[data-account-id="4"]').wait_for()
        self.assertEqual(self.card_order(page), ['2', '3', '0', '1', '4'])
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('[data-account-id="0"] .btn-switch').click()
        page.wait_for_function("document.querySelector('#list .acc').dataset.accountId === '0'")
        self.assertEqual(self.card_order(page), ['0', '3', '2', '1', '4'])
        self.assertEqual(payload['account_order'], ['3', '2', '0', '1', '4'])
        self.assertEqual([a['id'] for a in payload['accounts']], list('01234'))

    def test_drag_sort_and_active_pin(self):
        page, payload = self.open_ui(three_groups(), width=520)
        self.prepare_order_accounts(page, payload)
        self.assertEqual(page.locator('.acc-row:has(> .acc.current) > .order-handle').count(), 0)
        source = self.order_handle(page, '4')
        source.drag_to(page.locator('[data-account-id="0"]'), target_position={"x": 80, "y": 5})
        page.wait_for_function("state.data.account_order?.join() === '4,0,2,1,3'")
        self.assertEqual(self.card_order(page), ['2', '4', '0', '1', '3'])
        self.assertEqual(page.locator('.dragging,.drop-before,.drop-after').count(), 0)
        writes = len(self.order_requests)
        self.order_handle(page, '4').drag_to(page.locator('.acc.current'))
        self.assertEqual(len(self.order_requests), writes)
        self.assertEqual(self.card_order(page), ['2', '4', '0', '1', '3'])

    def test_sort_keyboard_bounds_and_failed_save(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload)
        first = page.locator('[data-account-id="0"]')
        self.order_handle(page, '0').click()
        self.assertTrue(first.locator('[data-action="move-up"]').is_disabled())
        page.keyboard.press('Escape')
        grip = self.order_handle(page, '0')
        grip.focus()
        page.keyboard.press('Alt+ArrowUp')
        self.assertEqual(self.order_requests, [])
        page.keyboard.press('Alt+ArrowDown')
        page.wait_for_function("state.data.account_order?.join() === '1,0,2,3,4' && !ui.orderSaving")
        self.assertEqual(page.evaluate('document.activeElement.dataset.focusKey'), 'order-0')
        page.keyboard.press('Alt+ArrowDown')
        page.wait_for_function("state.data.account_order?.join() === '1,3,2,0,4' && !ui.orderSaving")
        self.order_failure = True
        page.keyboard.press('Alt+ArrowDown')
        page.wait_for_function("!ui.orderSaving && document.querySelector('#toast').textContent.includes('fixture save failed')")
        self.assertEqual(self.card_order(page), ['2', '1', '3', '0', '4'])
        self.assertEqual(payload['account_order'], ['1', '3', '2', '0', '4'])
        self.assertEqual(page.locator('.order-handle:disabled').count(), 0)

    def test_stale_poll_cannot_overwrite_a_completed_reorder(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload)
        held = []
        snapshot = copy.deepcopy(payload)
        page.route('**/api/state', lambda request: held.append(request))
        with page.expect_request('**/api/state'):
            page.evaluate('() => { window.oldPollDone = false; loadState().then(() => window.oldPollDone = true); }')
        card = page.locator('[data-account-id="4"]')
        card.locator('.acc-menu-toggle').click()
        card.locator('[data-action="move-up"]').click()
        page.wait_for_function("state.data.account_order?.join() === '0,1,2,4,3' && !ui.orderSaving")
        self.assertEqual(len(held), 1)
        held[0].fulfill(json=snapshot)
        page.wait_for_function('window.oldPollDone')
        self.assertEqual(self.card_order(page), ['2', '0', '1', '4', '3'])

    def test_stale_preferences_keep_new_accounts_visible(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload, ['deleted', '2', '2', '4'])
        self.assertEqual(self.card_order(page), ['2', '4', '0', '1', '3'])
        payload['accounts'] = [a for a in payload['accounts'] if a['id'] != '4']
        payload['accounts'].append(dict(copy.deepcopy(payload['accounts'][0]), id='new', label='New account', current=False))
        page.evaluate('loadState()')
        page.locator('[data-account-id="new"]').wait_for()
        self.assertEqual(self.card_order(page), ['2', '0', '1', '3', 'new'])

    def test_refresh_all_old_snapshot_does_not_restore_previous_order(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload)
        held = []
        snapshot = copy.deepcopy(payload)
        page.route('**/api/refresh_all', lambda request: held.append(request))
        with page.expect_request('**/api/refresh_all'):
            page.locator('#btnRefreshAll').click()
        card = page.locator('[data-account-id="4"]')
        card.locator('.acc-menu-toggle').click()
        card.locator('[data-action="move-up"]').click()
        page.wait_for_function("state.data.account_order?.join() === '0,1,2,4,3' && !ui.orderSaving")
        held[0].fulfill(json=snapshot)
        page.wait_for_function("!document.querySelector('#btnRefreshAll').disabled")
        self.assertEqual(self.card_order(page), ['2', '0', '1', '4', '3'])
        self.assertEqual(payload['account_order'], ['0', '1', '2', '4', '3'])

    def test_leading_handle_and_compact_collapsed_card(self):
        for width in (320, 360, 520):
            with self.subTest(width=width):
                page, payload = self.open_ui(shared_groups(), width=width)
                self.prepare_order_accounts(page, payload)
                account = payload['accounts'][0]
                account['label'] = account['email']
                page.reload()
                card = page.locator('[data-account-id="0"]')
                card.wait_for()
                self.assertEqual(card.locator('.order-handle').count(), 0)
                self.assertEqual(card.locator('.acc-ghost-actions button:visible').count(), 2)
                handle = self.order_handle(page, '0').bounding_box()
                bounds = card.bounding_box()
                self.assertLessEqual(handle['x'] + handle['width'], bounds['x'] - 1)
                self.assertLessEqual(bounds['height'], 114)
                self.assertLessEqual(card.evaluate('e => e.scrollWidth - e.clientWidth'), 1)
                spacing = card.evaluate('''e => {
                    const sub = e.querySelector('.acc-subline').getBoundingClientRect();
                    const footer = e.querySelector('.acc-footer').getBoundingClientRect();
                    return {gap: footer.top - sub.bottom, bottom: e.getBoundingClientRect().bottom - footer.bottom};
                }''')
                self.assertLessEqual(spacing['gap'], 1)
                self.assertLessEqual(spacing['bottom'], 10)
                card.locator('.acc-expand').click()
                overflow = card.locator('.quota-table td:not(.qmissing)').evaluate_all('''cells => cells.some(e => {
                    const time = e.querySelector('.qreset').getBoundingClientRect();
                    const pct = e.querySelector('.qpct').getBoundingClientRect();
                    return time.right + 3 > pct.left || pct.right > e.getBoundingClientRect().right + 1;
                })''')
                self.assertFalse(overflow)

    def test_account_marks_have_different_main_shapes_and_header_has_visible_gray(self):
        page, _ = self.open_ui(three_groups())
        result = page.evaluate('''() => {
            const root = document.createElement('div');
            root.innerHTML = Array.from({length: 64}, (_, i) => accountMark('sample-' + i)).join('');
            const families = [...new Set([...root.querySelectorAll('svg')].map(e => e.dataset.markFamily))];
            const shapes = [...new Set([...root.querySelectorAll('svg')].map(e => e.innerHTML))];
            return {families: families.length, shapes: shapes.length,
                    header: getComputedStyle(document.querySelector('.quota-table thead th')).backgroundColor};
        }''')
        self.assertEqual(result['families'], 16)
        self.assertEqual(result['shapes'], 16)
        self.assertEqual(result['header'], 'rgb(233, 237, 242)')

    def test_oauth_poll_old_snapshot_does_not_restore_previous_order(self):
        page, payload = self.open_ui(three_groups())
        self.prepare_order_accounts(page, payload)
        held = []
        snapshot = copy.deepcopy(payload)
        page.route('**/api/state', lambda request: held.append(request))
        page.route('**/api/oauth_start', lambda request: request.fulfill(json={'auth_url': 'https://example.invalid/fixture-oauth'}))
        page.evaluate('''() => {
            window.__TAURI__ = {opener: {openUrl: async () => {}}};
            window.setInterval = fn => { window.fixtureOAuthTick = fn; return 123456; };
        }''')
        page.locator('#btnAddMenu').click()
        page.locator('#btnOauth').click()
        page.wait_for_function('typeof window.fixtureOAuthTick === "function"')
        with page.expect_request('**/api/state'):
            page.evaluate('() => { window.oauthTickDone = false; window.fixtureOAuthTick().then(() => window.oauthTickDone = true); }')
        card = page.locator('[data-account-id="4"]')
        card.locator('.acc-menu-toggle').click()
        card.locator('[data-action="move-up"]').click()
        page.wait_for_function("state.data.account_order?.join() === '0,1,2,4,3' && !ui.orderSaving")
        held[0].fulfill(json=snapshot)
        page.wait_for_function('window.oauthTickDone')
        self.assertEqual(self.card_order(page), ['2', '0', '1', '4', '3'])
        self.assertEqual(payload['account_order'], ['0', '1', '2', '4', '3'])

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
        output = Path(__file__).resolve().parents[1] / '.ref' / 'account-polish-v1.3.1'
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
