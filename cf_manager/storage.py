from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, TypeVar

from .models import Account, CnameTarget

T = TypeVar("T")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$",
    re.IGNORECASE,
)
NS_LINE_RE = re.compile(r"^(\S+)\s+(.+?)\s+\[(.+?)]$")


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError(f"Некорректный домен: {value}")
    return domain


def unique_domains(values: Iterable[str], *, strict: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            domain = normalize_domain(raw)
        except ValueError:
            if strict:
                raise
            continue
        if domain not in seen:
            seen.add(domain)
            result.append(domain)
    return result


class Storage:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.accounts_path = self.root / "dex_accounts.json"
        self.cnames_path = self.root / "dex_cnames.json"
        self.domains_path = self.root / "domains.txt"
        self.failed_path = self.root / "failed_domains.txt"
        self.logs_path = self.root / "logs"
        self.ns_path = self.logs_path / "ns_results.txt"

    def _load_json(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, list):
            raise ValueError(f"{path.name} должен содержать JSON-массив")
        return value

    def _save_json(self, path: Path, value: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load_accounts(self) -> list[Account]:
        return [Account.from_dict(item) for item in self._load_json(self.accounts_path)]

    def save_accounts(self, accounts: Iterable[Account]) -> None:
        items = list(accounts)
        identities = [(item.email.lower(), item.api_key) for item in items]
        if len(identities) != len(set(identities)):
            raise ValueError("Обнаружены дубли аккаунтов")
        self._save_json(self.accounts_path, [item.to_dict() for item in items])

    def load_cnames(self) -> list[CnameTarget]:
        return [CnameTarget.from_dict(item) for item in self._load_json(self.cnames_path)]

    def save_cnames(self, cnames: Iterable[CnameTarget]) -> None:
        items = list(cnames)
        if len({item.target.lower() for item in items}) != len(items):
            raise ValueError("Обнаружены дубли CNAME")
        self._save_json(self.cnames_path, [item.to_dict() for item in items])

    def load_domains(self, path: Path | str | None = None) -> list[str]:
        source = Path(path) if path else self.domains_path
        if not source.is_absolute():
            source = self.root / source
        if not source.exists():
            return []
        return unique_domains(source.read_text(encoding="utf-8").splitlines())

    def save_domains(self, domains: Iterable[str], path: Path | str | None = None) -> None:
        target = Path(path) if path else self.domains_path
        if not target.is_absolute():
            target = self.root / target
        normalized = unique_domains(domains, strict=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"{domain}\n" for domain in normalized), encoding="utf-8")

    def save_failed(self, domains: Iterable[str]) -> None:
        self.save_domains(domains, self.failed_path)

    def append_ns_header(self, account_label: str) -> None:
        self.logs_path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.ns_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n# ── {stamp}  [{account_label}] ──────────────────────────────\n")

    def append_ns(self, domain: str, name_servers: Iterable[str], account_label: str) -> None:
        servers = tuple(name_servers)
        if not servers:
            return
        self.logs_path.mkdir(parents=True, exist_ok=True)
        line = f"{domain:<40} {', '.join(servers):<60} [{account_label}]\n"
        with self.ns_path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def load_ns_history(self) -> dict[str, list[tuple[str, str]]]:
        result: dict[str, list[tuple[str, str]]] = defaultdict(list)
        if not self.ns_path.exists():
            return {}
        for line in self.ns_path.read_text(encoding="utf-8").splitlines():
            match = NS_LINE_RE.match(line.strip())
            if match:
                domain, servers, label = match.groups()
                result[label].append((domain, servers.strip()))
        return dict(result)

    def export_rows(self, filename: str, rows: Iterable[str]) -> Path:
        target = self.root / filename
        target.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
        return target
