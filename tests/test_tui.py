import json

import pytest

from cf_manager.models import Zone
from cf_manager.tui import CloudflareManagerApp


@pytest.fixture
def app_root(tmp_path):
    (tmp_path / "dex_accounts.json").write_text(
        json.dumps(
            [
                {
                    "label": "Primary",
                    "email": "user@example.com",
                    "api_key": "abcdefghijklmnopqrstuvwxyz",
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "dex_cnames.json").write_text(
        json.dumps([{"name": "Main", "target": "target.example.com"}]),
        encoding="utf-8",
    )
    (tmp_path / "domains.txt").write_text("one.example\ntwo.example\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_tui_mounts_every_workflow(app_root):
    app = CloudflareManagerApp(app_root, auto_analyze=False)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert len(app.query("BasePanel")) == 9
        assert app.query_one("#accounts-table").row_count == 1
        assert app.query_one("#cnames-table").row_count == 1
        assert app.query_one("#content").current == "dashboard"

        for panel in (
            "add-zone",
            "cleaner",
            "accounts",
            "cnames",
            "domains",
            "ns-history",
            "zones",
            "search",
        ):
            app.action_open_panel(panel)
            await pilot.pause()
            assert app.query_one("#content").current == panel


@pytest.mark.asyncio
async def test_api_key_is_not_rendered_in_accounts_table(app_root):
    app = CloudflareManagerApp(app_root, auto_analyze=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        row = app.query_one("#accounts-table").get_row_at(0)
        assert row[2] == "abcd••••wxyz"
        assert "efghijkl" not in str(row)


@pytest.mark.asyncio
async def test_search_accepts_multiple_domains_and_has_cname_action(app_root):
    app = CloudflareManagerApp(app_root, auto_analyze=False)
    app.zone_cache = [
        Zone("one.example", "active", "Primary", "user@example.com"),
        Zone("two.example", "active", "Primary", "user@example.com"),
        Zone("other.example", "active", "Primary", "user@example.com"),
    ]
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        app.action_open_panel("search")
        search = app.query_one("#search-filter")
        search.value = "one.example, two.example"
        await pilot.pause()

        assert app.query_one("#search-table").row_count == 2
        assert app.query_one("#search-replace-cname") is not None
        assert app.query_one("#search-new-cname").value == ""
        assert (
            "Main — target.example.com",
            "target.example.com",
        ) in app.query_one("#search-cname")._options


@pytest.mark.asyncio
async def test_live_zones_filter_by_team_sort_cname_and_select_bans(app_root):
    app = CloudflareManagerApp(app_root, auto_analyze=False)
    app.zone_cache = [
        Zone(
            "b.example",
            "active",
            "Alpha @One",
            "one@example.com",
            zone_id="z1",
            current_cname="z-target.example",
            cname_loaded=True,
            phishing_detected=True,
        ),
        Zone(
            "a.example",
            "active",
            "Alpha @Two",
            "two@example.com",
            zone_id="z2",
            current_cname="a-target.example",
            cname_loaded=True,
        ),
        Zone(
            "other.example",
            "active",
            "Beta @One",
            "beta@example.com",
            zone_id="z3",
        ),
    ]
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        panel = app.query_one("#zones")
        app.query_one("#zones-team").value = "Alpha"
        app.query_one("#zones-sort").value = "cname"
        await pilot.pause()

        assert [zone.name for zone in panel.visible_zones] == [
            "a.example",
            "b.example",
        ]
        panel.select_all_banned()
        assert panel.selected_zone_ids == {("one@example.com", "z1")}
