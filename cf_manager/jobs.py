from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from .models import Account, JobProgress, JobResult, Zone
from .services import CloudflareClient
from .storage import Storage

ProgressCallback = Callable[[JobProgress, JobResult], None]
LogCallback = Callable[[str], None]


@dataclass(slots=True)
class JobRunner:
    storage: Storage
    log: LogCallback = lambda _message: None
    progress: JobProgress | None = None
    _cancelled: bool = field(default=False, init=False)

    def cancel(self) -> None:
        self._cancelled = True
        if self.progress:
            self.progress.cancelled = True
        self.log("Запрошена отмена задания")

    def reset(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    async def _run(
        self,
        operations: list[Callable[[], Coroutine[Any, Any, JobResult]]],
        *,
        concurrency: int,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        self.reset()
        self.progress = JobProgress(total=len(operations))
        semaphore = asyncio.Semaphore(max(1, concurrency))
        lock = asyncio.Lock()

        async def execute(operation: Callable[[], Coroutine[Any, Any, JobResult]]) -> None:
            async with semaphore:
                if self.cancelled:
                    return
                result = await operation()
                async with lock:
                    assert self.progress is not None
                    self.progress.record(result)
                    if on_progress:
                        on_progress(self.progress, result)

        tasks = [asyncio.create_task(execute(operation)) for operation in operations]
        await asyncio.gather(*tasks, return_exceptions=False)
        assert self.progress is not None
        return self.progress

    async def add_domains(
        self,
        account: Account,
        domains: list[str],
        cname: str,
        *,
        concurrency: int = 10,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        client = CloudflareClient(dry_run=dry_run, log=self.log)
        if not dry_run:
            self.storage.append_ns_header(account.label)
        operations = [
            lambda domain=domain: client.add_domain(
                account, domain, cname, cancelled=lambda: self.cancelled
            )
            for domain in domains
        ]
        result = await self._run(
            operations, concurrency=concurrency, on_progress=on_progress
        )
        failed: list[str] = []
        for item in result.results:
            if item.success and item.name_servers and not dry_run:
                self.storage.append_ns(item.domain, item.name_servers, item.account)
            elif not item.success and item.message != "Отменено":
                failed.append(item.domain)
        self.storage.save_failed(failed)
        return result

    async def clean_domains(
        self,
        accounts: list[Account],
        domains: list[str],
        *,
        delete_zone: bool,
        concurrency: int = 5,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        client = CloudflareClient(dry_run=dry_run, log=self.log)
        operations = [
            lambda account=account, domain=domain: client.clean_domain(
                account,
                domain,
                delete_zone=delete_zone,
                cancelled=lambda: self.cancelled,
            )
            for account in accounts
            for domain in domains
        ]
        return await self._run(
            operations, concurrency=concurrency, on_progress=on_progress
        )

    async def replace_cnames(
        self,
        items: list[tuple[Account, str]],
        target: str,
        *,
        concurrency: int = 5,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        client = CloudflareClient(dry_run=dry_run, log=self.log)
        operations = [
            lambda account=account, domain=domain: client.replace_cname(
                account,
                domain,
                target,
                cancelled=lambda: self.cancelled,
            )
            for account, domain in items
        ]
        return await self._run(
            operations, concurrency=concurrency, on_progress=on_progress
        )

    async def delete_zones(
        self,
        items: list[tuple[Account, Zone]],
        *,
        concurrency: int = 8,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        client = CloudflareClient(dry_run=dry_run, log=self.log)
        operations = [
            lambda account=account, zone=zone: client.delete_zone(
                account,
                zone.zone_id,
                zone.name,
                cancelled=lambda: self.cancelled,
            )
            for account, zone in items
        ]
        return await self._run(
            operations, concurrency=concurrency, on_progress=on_progress
        )

    async def retry_failed_add(
        self,
        account: Account,
        cname: str,
        *,
        concurrency: int = 5,
        dry_run: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> JobProgress:
        return await self.add_domains(
            account,
            self.storage.load_domains(self.storage.failed_path),
            cname,
            concurrency=concurrency,
            dry_run=dry_run,
            on_progress=on_progress,
        )
