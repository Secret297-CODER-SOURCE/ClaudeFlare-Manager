import sys

import cloudflare_manager


def test_classic_flag_keeps_legacy_fallback(monkeypatch):
    called = []
    monkeypatch.setattr(cloudflare_manager, "main_classic", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["cloudflare_manager.py", "--classic"])

    cloudflare_manager.main()

    assert called == [True]
