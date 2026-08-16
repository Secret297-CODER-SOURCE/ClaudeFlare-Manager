import json

import pytest

from cf_manager.models import Account, CnameTarget, account_team
from cf_manager.storage import Storage, normalize_domain, unique_domains


def test_storage_round_trip_is_compatible(tmp_path):
    storage = Storage(tmp_path)
    accounts = [
        Account(label="Primary", email="user@example.com", api_key="secret-key")
    ]
    cnames = [CnameTarget(name="Main", target="target.example.com")]

    storage.save_accounts(accounts)
    storage.save_cnames(cnames)
    storage.save_domains(["Example.COM", "example.com", "second.example"])

    assert storage.load_accounts() == accounts
    assert storage.load_cnames() == cnames
    assert storage.load_domains() == ["example.com", "second.example"]
    raw = json.loads(storage.accounts_path.read_text(encoding="utf-8"))
    assert raw == [
        {
            "label": "Primary",
            "email": "user@example.com",
            "api_key": "secret-key",
        }
    ]


def test_account_key_is_masked():
    account = Account("Main", "user@example.com", "abcdefghijklmnop")
    assert account.masked_key == "abcd••••mnop"
    assert "efgh" not in account.masked_key


def test_domain_validation_and_deduplication():
    assert normalize_domain("Example.COM.") == "example.com"
    assert unique_domains(["one.example", "ONE.EXAMPLE", "# comment", "bad"]) == [
        "one.example"
    ]
    with pytest.raises(ValueError):
        normalize_domain("not a domain")


def test_ns_history_parser(tmp_path):
    storage = Storage(tmp_path)
    storage.append_ns_header("Primary")
    storage.append_ns(
        "example.com",
        ["alice.ns.cloudflare.com", "bob.ns.cloudflare.com"],
        "Primary",
    )
    assert storage.load_ns_history() == {
        "Primary": [
            (
                "example.com",
                "alice.ns.cloudflare.com, bob.ns.cloudflare.com",
            )
        ]
    }


def test_team_is_derived_from_account_label_prefix():
    assert account_team("611 YAN @DED_CARAMEL") == "611 YAN"
    assert account_team("Bravo @dobro") == "Bravo"
    assert account_team("person@example.com") == "Без команды"
