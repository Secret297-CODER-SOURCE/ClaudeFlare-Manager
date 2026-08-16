import pytest

from cf_manager.models import Account
from cf_manager.services import CloudflareClient, find_zones_across_accounts


@pytest.mark.asyncio
async def test_replace_cname_only_removes_conflicting_apex_records(monkeypatch):
    client = CloudflareClient()
    account = Account("Main", "user@example.com", "key")
    calls = []

    async def find_zone(session, selected_account, domain):
        return {"id": "zone-1"}

    async def request(session, method, path, selected_account, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "success": True,
                "result": [
                    {"id": "a-1", "type": "A", "name": "example.com"},
                    {"id": "mx-1", "type": "MX", "name": "example.com"},
                    {"id": "sub-1", "type": "A", "name": "www.example.com"},
                ],
            }
        return {"success": True, "result": {}}

    monkeypatch.setattr(client, "_find_zone", find_zone)
    monkeypatch.setattr(client, "request", request)

    result = await client.replace_cname(
        account, "example.com", "target.example.com"
    )

    deleted_paths = [path for method, path, _ in calls if method == "DELETE"]
    assert deleted_paths == ["/zones/zone-1/dns_records/a-1"]
    post = next(item for item in calls if item[0] == "POST")
    assert post[2]["body"]["content"] == "target.example.com"
    assert result.success


@pytest.mark.asyncio
async def test_cross_account_search_reports_progress(monkeypatch):
    client = CloudflareClient()
    accounts = [
        Account("One", "one@example.com", "key"),
        Account("Two", "two@example.com", "key"),
    ]
    updates = []

    async def find_zones(account, domains, session=None):
        from cf_manager.models import Zone

        return (
            [Zone(domains[0], "active", account.label, account.email)]
            if account.label == "Two"
            else []
        )

    monkeypatch.setattr(client, "find_zones", find_zones)
    zones, errors = await find_zones_across_accounts(
        client,
        accounts,
        ["example.com"],
        progress=lambda done, total, label, found, error: updates.append(
            (done, total, label, found, error)
        ),
    )

    assert len(updates) == 2
    assert updates[-1][0:2] == (2, 2)
    assert updates[-1][3] == 1
    assert len(zones) == 1
    assert errors == []


@pytest.mark.asyncio
async def test_exact_search_reads_current_apex_cname(monkeypatch):
    client = CloudflareClient()
    account = Account("Main", "user@example.com", "key")

    async def request(session, method, path, selected_account, **kwargs):
        if path == "/zones":
            return {
                "success": True,
                "result": [
                    {
                        "id": "zone-1",
                        "name": "example.com",
                        "status": "active",
                        "name_servers": [],
                        "meta": {"phishing_detected": True},
                    }
                ],
            }
        return {
            "success": True,
            "result": [
                {
                    "id": "cname-1",
                    "type": "CNAME",
                    "name": "example.com",
                    "content": "old-target.example.com",
                }
            ],
        }

    monkeypatch.setattr(client, "request", request)
    zones = await client.find_zones(account, ["example.com"])

    assert len(zones) == 1
    assert zones[0].current_cname == "old-target.example.com"
    assert zones[0].phishing_detected
