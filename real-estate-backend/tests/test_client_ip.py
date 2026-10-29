"""The visitor IP (the clinic demo's client_ip): X-Real-IP only from a trusted
proxy peer (TRUST_PROXY on and a loopback socket peer), X-Forwarded-For never,
an unparseable peer as 0.0.0.0. A forged header can neither change the IP a
session is stored and reused under nor dodge the per-IP cap."""

import httpx
import pytest
from sqlalchemy import select
from starlette.requests import Request

from app import main
from app.config import settings
from app.db import SessionLocal
from app.deps import client_ip, proxy_country
from app.main import app
from app.models import DemoSession, IpUsage
from conftest import add_session

VICTIM = "10.9.9.9"
FORGED = {"X-Real-IP": VICTIM, "X-Forwarded-For": VICTIM, "CF-IPCountry": "DE"}


def _request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 50000) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture
def behind_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)


async def _open(client, api_key, headers=None) -> str:
    r = await client.post("/api/session", headers={"X-API-Key": api_key, **(headers or {})})
    assert r.status_code == 200
    return r.json()["session_id"]


async def _ip_of(sid: str) -> str | None:
    async with SessionLocal() as s:
        ip = (await s.get(DemoSession, sid)).ip
    return str(ip) if ip is not None else None


# ---- client_ip / proxy_country ----


@pytest.mark.parametrize("trust", [False, True])
@pytest.mark.parametrize("peer", ["10.1.2.3", "192.168.0.10", "2001:db8::5"])
def test_an_untrusted_peer_cannot_forge_its_ip(monkeypatch, trust, peer):
    monkeypatch.setattr(settings, "trust_proxy", trust)
    req = _request(peer, FORGED)
    assert client_ip(req) == peer
    assert proxy_country(req) is None


def test_loopback_is_not_trusted_without_trust_proxy():
    assert settings.trust_proxy is False
    req = _request("127.0.0.1", FORGED)
    assert client_ip(req) == "127.0.0.1"
    assert proxy_country(req) is None


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
def test_a_trusted_peers_x_real_ip_is_used(behind_proxy, peer):
    req = _request(peer, {"X-Real-IP": "203.0.113.7", "CF-IPCountry": "IN"})
    assert client_ip(req) == "203.0.113.7"
    assert proxy_country(req) == "IN"


def test_x_forwarded_for_is_never_read(behind_proxy):
    assert client_ip(_request("127.0.0.1", {"X-Forwarded-For": "203.0.113.7"})) == "127.0.0.1"


@pytest.mark.parametrize("value", ["garbage", "203.0.113.7, 10.0.0.1", "999.1.1.1", " "])
def test_a_garbage_x_real_ip_falls_back_to_the_peer(behind_proxy, value):
    assert client_ip(_request("127.0.0.1", {"X-Real-IP": value})) == "127.0.0.1"


@pytest.mark.parametrize("peer", ["unknown", "testclient", "", None])
def test_an_unparseable_peer_is_0_0_0_0(peer):
    assert client_ip(_request(peer)) == "0.0.0.0"


# ---- through the routes ----


@pytest.mark.parametrize("trust", [False, True])
async def test_a_forged_header_does_not_return_another_ips_session(
    client_from, api_key, monkeypatch, trust
):
    monkeypatch.setattr(settings, "trust_proxy", trust)
    victim = await add_session(VICTIM, agent_session_id="agent-victim")
    attacker = client_from("10.1.2.3")
    sid = await _open(attacker, api_key, FORGED)
    assert sid != victim
    assert await _ip_of(sid) == "10.1.2.3"
    async with SessionLocal() as s:
        assert (await s.get(DemoSession, sid)).country is None


async def test_a_trusted_proxy_peer_gets_the_real_visitors_session(
    client, api_key, behind_proxy
):
    # The default test client's peer is 127.0.0.1, i.e. nginx on the same host.
    live = await add_session("203.0.113.7")
    assert await _open(client, api_key, {"X-Real-IP": "203.0.113.7"}) == live
    fresh = await _open(client, api_key, {"X-Real-IP": "198.51.100.4", "CF-IPCountry": "IN"})
    assert fresh != live and await _ip_of(fresh) == "198.51.100.4"
    async with SessionLocal() as s:
        assert (await s.get(DemoSession, fresh)).country == "IN"


async def test_a_forged_header_does_not_dodge_the_cap(client_from, api_key, monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy", True)
    monkeypatch.setattr(settings, "chat_daily_ip_cap", 1)

    async def reply(text, session_id, resume=None):
        return "ok", "agent-1"

    monkeypatch.setattr(main, "generate_reply", reply)
    attacker = client_from("10.1.2.3")
    sid = await _open(attacker, api_key)
    replies = []
    for n in range(2):
        r = await attacker.post(
            "/api/chat", json={"message": "hi"},
            headers={"X-API-Key": api_key, "X-Session-Id": sid,
                     "X-Real-IP": f"10.8.0.{n}", "X-Forwarded-For": f"10.8.0.{n}"},
        )
        replies.append(r.json()["reply"])
    assert replies[0] == "ok"
    assert replies[1].startswith("You've reached today's message limit")
    async with SessionLocal() as s:
        usage = [(u.ip, u.count, u.blocked) for u in (await s.execute(select(IpUsage))).scalars()]
    assert usage == [("10.1.2.3", 1, 1)]


async def test_an_unparseable_peer_still_opens_a_session(api_key):
    transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        sid = await _open(ac, api_key)
    # 0.0.0.0 is no visitor address: no IP stored, so nothing is shared by it.
    assert await _ip_of(sid) is None
