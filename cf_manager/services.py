from __future__ import annotations

import asyncio
import random
import ssl
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import aiohttp
import certifi

from .models import Account, JobResult, Zone

BASE_URL = "https://api.cloudflare.com/client/v4"
LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]


class CloudflareError(RuntimeError):
    pass


class CloudflareClient:
    def __init__(
        self,
        *,
        timeout: float = 30,
        retries: int = 5,
        retry_delay: float = 2,
        dry_run: bool = False,
        log: LogCallback | None = None,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.retries = retries
        self.retry_delay = retry_delay
        self.dry_run = dry_run
        self.log = log or (lambda _message: None)
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        return aiohttp.ClientSession(timeout=self.timeout, connector=connector)

    @staticmethod
    def headers(account: Account) -> dict[str, str]:
        return {
            "X-Auth-Email": account.email,
            "X-Auth-Key": account.api_key,
            "Content-Type": "application/json",
        }

    async def request(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        account: Account,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                async with session.request(
                    method,
                    url,
                    headers=self.headers(account),
                    params=params,
                    json=body,
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status == 429:
                        delay = self.retry_delay * attempt + random.uniform(0, 0.5)
                        self.log(f"Лимит API, повтор через {delay:.1f} сек.")
                        await asyncio.sleep(delay)
                        continue
                    if response.status >= 400 or not data.get("success", False):
                        errors = data.get("errors") or [{"message": response.reason}]
                        message = "; ".join(str(item.get("message", item)) for item in errors)
                        raise CloudflareError(f"HTTP {response.status}: {message}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last_error = error
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_delay * attempt)
            except CloudflareError:
                raise
        raise CloudflareError(f"Cloudflare API недоступен: {last_error}")

    async def list_zones(
        self,
        account: Account,
        session: aiohttp.ClientSession | None = None,
    ) -> list[Zone]:
        if session is None:
            async with self.session() as owned_session:
                return await self.list_zones(account, owned_session)
        first_page = await self.request(
            session,
            "GET",
            "/zones",
            account,
            params={"per_page": 50, "page": 1},
        )
        total_pages = int(
            (first_page.get("result_info") or {}).get("total_pages", 1)
        )
        page_semaphore = asyncio.Semaphore(6)

        async def fetch_page(page: int) -> dict[str, Any]:
            async with page_semaphore:
                return await self.request(
                    session,
                    "GET",
                    "/zones",
                    account,
                    params={"per_page": 50, "page": page},
                )

        remaining_pages = (
            await asyncio.gather(
                *(fetch_page(page) for page in range(2, total_pages + 1))
            )
            if total_pages > 1
            else []
        )
        zones: list[Zone] = []
        for data in [first_page, *remaining_pages]:
            for item in data.get("result", []):
                zones.append(
                    Zone(
                        name=str(item.get("name", "")),
                        status=str(item.get("status", "unknown")),
                        account_label=account.label,
                        account_email=account.email,
                        zone_id=str(item.get("id", "")),
                        name_servers=tuple(item.get("name_servers") or ()),
                        phishing_detected=bool(
                            (item.get("meta") or {}).get("phishing_detected")
                        ),
                    )
                )
        return zones

    async def find_zones(
        self,
        account: Account,
        domains: list[str],
        session: aiohttp.ClientSession | None = None,
    ) -> list[Zone]:
        """Find exact zone names without downloading the whole account."""
        if session is None:
            async with self.session() as owned_session:
                return await self.find_zones(account, domains, owned_session)
        zones: list[Zone] = []
        for domain in domains:
            data = await self.request(
                session,
                "GET",
                "/zones",
                account,
                params={"name": domain, "per_page": 50},
            )
            for item in data.get("result", []):
                zone_name = str(item.get("name", ""))
                zone_id = str(item.get("id", ""))
                current_cname = ""
                try:
                    dns_data = await self.request(
                        session,
                        "GET",
                        f"/zones/{zone_id}/dns_records",
                        account,
                        params={
                            "type": "CNAME",
                            "name": zone_name,
                            "per_page": 100,
                        },
                    )
                    current_cname = next(
                        (
                            str(record.get("content", ""))
                            for record in dns_data.get("result", [])
                            if record.get("type") == "CNAME"
                            and str(record.get("name", "")).rstrip(".").lower()
                            == zone_name.rstrip(".").lower()
                        ),
                        "",
                    )
                except CloudflareError as error:
                    self.log(f"{zone_name}: CNAME не прочитан — {error}")
                zones.append(
                    Zone(
                        name=zone_name,
                        status=str(item.get("status", "unknown")),
                        account_label=account.label,
                        account_email=account.email,
                        zone_id=zone_id,
                        name_servers=tuple(item.get("name_servers") or ()),
                        current_cname=current_cname,
                        cname_loaded=True,
                        phishing_detected=bool(
                            (item.get("meta") or {}).get("phishing_detected")
                        ),
                    )
                )
        return zones

    async def _find_zone(
        self, session: aiohttp.ClientSession, account: Account, domain: str
    ) -> dict[str, Any] | None:
        data = await self.request(
            session, "GET", "/zones", account, params={"name": domain}
        )
        result = data.get("result") or []
        return result[0] if result else None

    async def add_domain(
        self,
        account: Account,
        domain: str,
        cname: str,
        *,
        cancelled: CancelCheck | None = None,
    ) -> JobResult:
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return JobResult(domain, account.label, False, "Отменено")
        if self.dry_run:
            self.log(f"[DRY RUN] {domain}: зона, SSL flexible и CNAME → {cname}")
            return JobResult(domain, account.label, True, "Dry-run")
        try:
            async with self.session() as session:
                try:
                    data = await self.request(
                        session,
                        "POST",
                        "/zones",
                        account,
                        body={"name": domain, "jump_start": True},
                    )
                    zone = data["result"]
                    self.log(f"{domain}: зона добавлена")
                except CloudflareError:
                    zone = await self._find_zone(session, account, domain)
                    if not zone:
                        raise
                    self.log(f"{domain}: используется существующая зона")
                zone_id = str(zone["id"])
                if cancelled():
                    return JobResult(domain, account.label, False, "Отменено")
                await self.request(
                    session,
                    "PATCH",
                    f"/zones/{zone_id}/settings/ssl",
                    account,
                    body={"value": "flexible"},
                )
                records = await self.request(
                    session, "GET", f"/zones/{zone_id}/dns_records", account
                )
                for record in records.get("result", []):
                    if cancelled():
                        return JobResult(domain, account.label, False, "Отменено")
                    await self.request(
                        session,
                        "DELETE",
                        f"/zones/{zone_id}/dns_records/{record['id']}",
                        account,
                    )
                await self.request(
                    session,
                    "POST",
                    f"/zones/{zone_id}/dns_records",
                    account,
                    body={
                        "type": "CNAME",
                        "name": "@",
                        "content": cname,
                        "ttl": 1,
                        "proxied": True,
                    },
                )
                details = await self.request(
                    session, "GET", f"/zones/{zone_id}", account
                )
                name_servers = tuple(details.get("result", {}).get("name_servers") or ())
                self.log(f"{domain}: готово")
                return JobResult(
                    domain, account.label, True, "Добавлен", name_servers
                )
        except Exception as error:
            self.log(f"{domain}: ошибка — {error}")
            return JobResult(domain, account.label, False, str(error))

    async def clean_domain(
        self,
        account: Account,
        domain: str,
        *,
        delete_zone: bool,
        cancelled: CancelCheck | None = None,
    ) -> JobResult:
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return JobResult(domain, account.label, False, "Отменено")
        try:
            async with self.session() as session:
                zone = await self._find_zone(session, account, domain)
                if not zone:
                    return JobResult(domain, account.label, False, "Зона не найдена")
                zone_id = str(zone["id"])
                page = 1
                records: list[dict[str, Any]] = []
                while True:
                    data = await self.request(
                        session,
                        "GET",
                        f"/zones/{zone_id}/dns_records",
                        account,
                        params={"per_page": 100, "page": page},
                    )
                    batch = data.get("result") or []
                    records.extend(batch)
                    if len(batch) < 100:
                        break
                    page += 1
                for record in records:
                    if cancelled():
                        return JobResult(domain, account.label, False, "Отменено")
                    if not self.dry_run:
                        await self.request(
                            session,
                            "DELETE",
                            f"/zones/{zone_id}/dns_records/{record['id']}",
                            account,
                        )
                if delete_zone and not self.dry_run:
                    await self.request(
                        session, "DELETE", f"/zones/{zone_id}", account
                    )
                action = "DNS и зона удалены" if delete_zone else "DNS очищен"
                if self.dry_run:
                    action = f"Dry-run: {action}"
                self.log(f"{domain}: {action}")
                return JobResult(domain, account.label, True, action)
        except Exception as error:
            self.log(f"{domain}: ошибка — {error}")
            return JobResult(domain, account.label, False, str(error))

    async def replace_cname(
        self,
        account: Account,
        domain: str,
        target: str,
        *,
        cancelled: CancelCheck | None = None,
    ) -> JobResult:
        """Replace only conflicting apex A/AAAA/CNAME records."""
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return JobResult(domain, account.label, False, "Отменено")
        if self.dry_run:
            self.log(f"[DRY RUN] {domain}: apex CNAME → {target}")
            return JobResult(domain, account.label, True, "Dry-run")
        try:
            async with self.session() as session:
                zone = await self._find_zone(session, account, domain)
                if not zone:
                    return JobResult(domain, account.label, False, "Зона не найдена")
                zone_id = str(zone["id"])
                data = await self.request(
                    session,
                    "GET",
                    f"/zones/{zone_id}/dns_records",
                    account,
                    params={"name": domain, "per_page": 100},
                )
                conflicts = [
                    record
                    for record in data.get("result", [])
                    if str(record.get("name", "")).rstrip(".").lower() == domain.lower()
                    and record.get("type") in {"A", "AAAA", "CNAME"}
                ]
                for record in conflicts:
                    if cancelled():
                        return JobResult(domain, account.label, False, "Отменено")
                    await self.request(
                        session,
                        "DELETE",
                        f"/zones/{zone_id}/dns_records/{record['id']}",
                        account,
                    )
                await self.request(
                    session,
                    "POST",
                    f"/zones/{zone_id}/dns_records",
                    account,
                    body={
                        "type": "CNAME",
                        "name": "@",
                        "content": target,
                        "ttl": 1,
                        "proxied": True,
                    },
                )
                message = f"CNAME → {target}"
                self.log(f"{domain}: {message}")
                return JobResult(domain, account.label, True, message)
        except Exception as error:
            self.log(f"{domain}: ошибка — {error}")
            return JobResult(domain, account.label, False, str(error))

    async def delete_zone(
        self,
        account: Account,
        zone_id: str,
        domain: str,
        *,
        cancelled: CancelCheck | None = None,
    ) -> JobResult:
        cancelled = cancelled or (lambda: False)
        if cancelled():
            return JobResult(domain, account.label, False, "Отменено")
        if self.dry_run:
            self.log(f"[DRY RUN] {domain}: удалить зону")
            return JobResult(domain, account.label, True, "Dry-run")
        try:
            async with self.session() as session:
                await self.request(
                    session, "DELETE", f"/zones/{zone_id}", account
                )
            self.log(f"{domain}: зона удалена")
            return JobResult(domain, account.label, True, "Зона удалена")
        except Exception as error:
            self.log(f"{domain}: ошибка удаления — {error}")
            return JobResult(domain, account.label, False, str(error))


async def list_all_zones(
    client: CloudflareClient,
    accounts: list[Account],
    *,
    concurrency: int = 16,
    progress: Callable[[int, int, str, int, str | None], None] | None = None,
) -> list[Zone]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    progress_lock = asyncio.Lock()
    completed = 0
    found = 0

    async def fetch(
        account: Account, session: aiohttp.ClientSession
    ) -> tuple[list[Zone], str | None]:
        nonlocal completed, found
        async with semaphore:
            try:
                batch = await client.list_zones(account, session)
                error = None
            except Exception as exc:
                batch = []
                error = f"{account.label}: {exc}"
                client.log(error)
            async with progress_lock:
                completed += 1
                found += len(batch)
                if progress:
                    progress(
                        completed,
                        len(accounts),
                        account.label,
                        found,
                        error,
                    )
            return batch, error

    async with client.session() as session:
        batches = await asyncio.gather(
            *(fetch(account, session) for account in accounts)
        )
    result = [zone for batch, _error in batches for zone in batch]
    return sorted(result, key=lambda zone: (zone.name, zone.account_label))


async def find_zones_across_accounts(
    client: CloudflareClient,
    accounts: list[Account],
    domains: list[str],
    *,
    concurrency: int = 8,
    progress: Callable[[int, int, str, int, str | None], None] | None = None,
) -> tuple[list[Zone], list[str]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    progress_lock = asyncio.Lock()
    completed = 0
    found = 0

    async def fetch(
        account: Account, session: aiohttp.ClientSession
    ) -> tuple[list[Zone], str | None]:
        nonlocal completed, found
        async with semaphore:
            try:
                batch = await client.find_zones(account, domains, session)
                error = None
            except Exception as exc:
                batch = []
                error = f"{account.label}: {exc}"
            async with progress_lock:
                completed += 1
                found += len(batch)
                if progress:
                    progress(
                        completed,
                        len(accounts),
                        account.label,
                        found,
                        error,
                    )
            return batch, error

    async with client.session() as session:
        batches = await asyncio.gather(
            *(fetch(account, session) for account in accounts)
        )
    zones = [zone for batch, _error in batches for zone in batch]
    errors = [error for _batch, error in batches if error]
    unique = {
        (zone.account_email, zone.name): zone
        for zone in zones
    }
    return (
        sorted(unique.values(), key=lambda zone: (zone.name, zone.account_label)),
        errors,
    )


async def load_cnames_for_zones(
    client: CloudflareClient,
    zones: list[Zone],
    accounts: list[Account],
    *,
    concurrency: int = 30,
    progress: Callable[[int, int, str, int, str | None], None] | None = None,
) -> tuple[list[Zone], list[str]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    progress_lock = asyncio.Lock()
    by_email = {account.email: account for account in accounts}
    by_label = {account.label: account for account in accounts}
    completed = 0
    with_cname = 0

    async def fetch(
        zone: Zone, session: aiohttp.ClientSession
    ) -> tuple[Zone, str | None]:
        nonlocal completed, with_cname
        account = by_email.get(zone.account_email) or by_label.get(
            zone.account_label
        )
        error: str | None = None
        updated = zone
        if account is None:
            error = f"{zone.name}: аккаунт не найден"
        else:
            async with semaphore:
                try:
                    data = await client.request(
                        session,
                        "GET",
                        f"/zones/{zone.zone_id}/dns_records",
                        account,
                        params={
                            "type": "CNAME",
                            "name": zone.name,
                            "per_page": 100,
                        },
                    )
                    current_cname = next(
                        (
                            str(record.get("content", ""))
                            for record in data.get("result", [])
                            if record.get("type") == "CNAME"
                            and str(record.get("name", "")).rstrip(".").lower()
                            == zone.name.rstrip(".").lower()
                        ),
                        "",
                    )
                    updated = replace(
                        zone, current_cname=current_cname, cname_loaded=True
                    )
                except Exception as exc:
                    error = f"{zone.name}: {exc}"
        async with progress_lock:
            completed += 1
            with_cname += int(bool(updated.current_cname))
            if progress:
                progress(
                    completed,
                    len(zones),
                    zone.name,
                    with_cname,
                    error,
                )
        return updated, error

    async with client.session() as session:
        results = await asyncio.gather(*(fetch(zone, session) for zone in zones))
    return (
        [zone for zone, _error in results],
        [error for _zone, error in results if error],
    )
