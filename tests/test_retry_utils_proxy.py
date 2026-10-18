"""Proxy-bypass hardening for ``retry_utils.setup_proxy_bypass``.

``httpx2`` parses each ``NO_PROXY`` / ``no_proxy`` entry as a URL pattern when a
``ChatOpenAI`` client is constructed. The bracketed IPv6 spelling ``[::1]`` is
parsed as a URL with an invalid port and raises ``httpx2.InvalidURL`` before any
request is attempted, so ``setup_proxy_bypass`` must rewrite it to the bare
``::1`` form it accepts.
"""
import os

import pytest

import retry_utils


def test_bracketed_ipv6_is_unwrapped_and_deduplicated(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")

    retry_utils.setup_proxy_bypass()

    value = os.environ["NO_PROXY"]
    assert "[" not in value
    assert "]" not in value
    entries = value.split(",")
    assert "::1" in entries
    assert len(entries) == len(set(entries))


def test_custom_domains_are_preserved(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "my.internal.corp,[::1]")

    retry_utils.setup_proxy_bypass()

    entries = os.environ["NO_PROXY"].split(",")
    assert "my.internal.corp" in entries
    assert "::1" in entries


def test_repeated_calls_are_idempotent(monkeypatch):
    monkeypatch.setenv(
        "NO_PROXY", "localhost,127.0.0.1,::1,[::1],my.internal.corp"
    )

    retry_utils.setup_proxy_bypass()
    first = os.environ["NO_PROXY"]
    retry_utils.setup_proxy_bypass()

    assert os.environ["NO_PROXY"] == first


def test_both_spellings_are_normalized(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,[::1]")

    retry_utils.setup_proxy_bypass()

    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ[key]
        assert "[" not in value
        assert "]" not in value
        assert "::1" in value.split(",")


def test_normalized_environment_constructs_chatopenai(monkeypatch):
    langchain_openai = pytest.importorskip("langchain_openai")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1,[::1]")

    retry_utils.setup_proxy_bypass()

    for key in ("NO_PROXY", "no_proxy"):
        assert "[" not in os.environ[key]
        assert "]" not in os.environ[key]

    langchain_openai.ChatOpenAI(model="m", api_key="p")
