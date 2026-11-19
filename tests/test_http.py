"""The HTTP front door: path, token, and the token file."""

import asyncio

import httpx

from hullforge_mcp import server as srv


def _client(token="good"):
    seen = []

    async def handle(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = srv.http_app(handle, token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"), seen


def test_token_required():
    async def go():
        c, seen = _client()
        async with c:
            assert (await c.post("/mcp")).status_code == 401
            assert (await c.post("/mcp", headers={"Authorization": "Bearer bad"})).status_code == 401
            r = await c.post("/mcp", headers={"Authorization": "Bearer good"})
            assert r.status_code == 200 and seen == ["/mcp"]
    asyncio.run(go())


def test_only_mcp_path():
    async def go():
        c, seen = _client()
        async with c:
            assert (await c.get("/", headers={"Authorization": "Bearer good"})).status_code == 404
            assert seen == []
    asyncio.run(go())


def test_token_file_made_once(tmp_path, monkeypatch):
    monkeypatch.delenv("HULLFORGE_HTTP_TOKEN", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    a, where = srv.http_token()
    b, _ = srv.http_token()
    assert a == b and len(a) >= 32 and where.endswith("http-token")
    monkeypatch.setenv("HULLFORGE_HTTP_TOKEN", "x")
    assert srv.http_token() == ("x", "HULLFORGE_HTTP_TOKEN")
