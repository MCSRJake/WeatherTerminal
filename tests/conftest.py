"""Public-provider traffic is forbidden throughout the deterministic suite."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must inject a transport; real HTTP is forbidden")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    # Leave asyncio's Windows loopback/socket-pair initialization alone.
