import pytest

from cf_manager.jobs import JobRunner
from cf_manager.models import Account, Zone
from cf_manager.storage import Storage


@pytest.mark.asyncio
async def test_add_domains_dry_run_reports_progress(tmp_path):
    storage = Storage(tmp_path)
    runner = JobRunner(storage)
    events = []

    result = await runner.add_domains(
        Account("Main", "user@example.com", "key"),
        ["one.example", "two.example"],
        "target.example.com",
        dry_run=True,
        on_progress=lambda progress, item: events.append(
            (progress.done, item.domain, item.success)
        ),
    )

    assert result.total == 2
    assert result.done == result.succeeded == 2
    assert result.failed == 0
    assert len(events) == 2
    assert storage.load_domains(storage.failed_path) == []


@pytest.mark.asyncio
async def test_cancelled_runner_skips_queued_work(tmp_path, monkeypatch):
    storage = Storage(tmp_path)
    runner = JobRunner(storage)

    async def slow_add(self, account, domain, cname, *, cancelled):
        from cf_manager.models import JobResult

        if domain == "one.example":
            runner.cancel()
        return JobResult(domain, account.label, not cancelled(), "done")

    monkeypatch.setattr(
        "cf_manager.services.CloudflareClient.add_domain", slow_add
    )
    result = await runner.add_domains(
        Account("Main", "user@example.com", "key"),
        ["one.example", "two.example", "three.example"],
        "target.example.com",
        concurrency=1,
    )

    assert result.cancelled
    assert result.done <= 1


@pytest.mark.asyncio
async def test_replace_cnames_dry_run_is_batchable(tmp_path):
    storage = Storage(tmp_path)
    runner = JobRunner(storage)
    account = Account("Main", "user@example.com", "key")

    result = await runner.replace_cnames(
        [(account, "one.example"), (account, "two.example")],
        "target.example.com",
        dry_run=True,
    )

    assert result.done == result.succeeded == 2
    assert all(item.message == "Dry-run" for item in result.results)


@pytest.mark.asyncio
async def test_banned_zones_can_be_deleted_in_dry_run(tmp_path):
    storage = Storage(tmp_path)
    runner = JobRunner(storage)
    account = Account("Team @One", "user@example.com", "key")
    zone = Zone(
        "blocked.example",
        "active",
        account.label,
        account.email,
        zone_id="zone-1",
        phishing_detected=True,
    )

    result = await runner.delete_zones([(account, zone)], dry_run=True)

    assert result.done == result.succeeded == 1
    assert result.results[0].message == "Dry-run"
