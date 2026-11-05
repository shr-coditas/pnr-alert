import os

import pnr_alert as p


def test_load_env_handles_bom(monkeypatch, tmp_path):
    monkeypatch.delenv("BOM_TEST_A", raising=False)
    # what a BOM-polluted cloud secret looks like: hidden BOM at the front, CRLF at the end
    monkeypatch.setenv("BOM_TEST_B", "﻿2026-11-07 22:00\r\n")
    monkeypatch.setattr(p, "__file__", str(tmp_path / "pnr_alert.py"))
    (tmp_path / ".env").write_bytes(b"\xef\xbb\xbfBOM_TEST_A=hello\n")
    p.load_env()
    assert os.environ["BOM_TEST_A"] == "hello"
    assert os.environ["BOM_TEST_B"] == "2026-11-07 22:00"
