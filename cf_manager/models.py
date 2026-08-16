from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Account:
    label: str
    email: str
    api_key: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Account":
        email = str(value.get("email", "")).strip()
        api_key = str(value.get("api_key", "")).strip()
        label = str(value.get("label", "")).strip() or email
        if not email or "@" not in email:
            raise ValueError("Укажите корректный email")
        if not api_key:
            raise ValueError("API-ключ не может быть пустым")
        return cls(label=label, email=email, api_key=api_key)

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "email": self.email, "api_key": self.api_key}

    @property
    def masked_key(self) -> str:
        if len(self.api_key) < 9:
            return "••••••••"
        return f"{self.api_key[:4]}••••{self.api_key[-4:]}"


@dataclass(frozen=True, slots=True)
class CnameTarget:
    name: str
    target: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CnameTarget":
        target = str(value.get("target", "")).strip().rstrip(".")
        name = str(value.get("name", "")).strip() or target
        if (
            not target
            or "." not in target
            or target.lower() in {"select.null", "none", "null"}
        ):
            raise ValueError("Укажите корректный CNAME target")
        return cls(name=name, target=target)

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "target": self.target}


@dataclass(frozen=True, slots=True)
class Zone:
    name: str
    status: str
    account_label: str
    account_email: str = ""
    zone_id: str = ""
    name_servers: tuple[str, ...] = ()
    current_cname: str = ""
    cname_loaded: bool = False
    phishing_detected: bool = False

    @property
    def team(self) -> str:
        return account_team(self.account_label)


def account_team(label: str) -> str:
    """Derive a stable team name from human account labels."""
    value = label.strip()
    if re.fullmatch(r"[^@\s]+@[^@\s]+", value):
        return "Без команды"
    if "@" in value:
        value = value.split("@", 1)[0].strip()
    value = re.sub(r"\s+\d+$", "", value).strip()
    return value or "Без команды"


@dataclass(slots=True)
class JobResult:
    domain: str
    account: str
    success: bool
    message: str
    name_servers: tuple[str, ...] = ()


@dataclass(slots=True)
class JobProgress:
    total: int
    done: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: bool = False
    results: list[JobResult] = field(default_factory=list)

    def record(self, result: JobResult) -> None:
        self.done += 1
        self.succeeded += int(result.success)
        self.failed += int(not result.success)
        self.results.append(result)

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else (self.done / self.total) * 100
