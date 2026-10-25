import hashlib
import importlib.util
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "local_check", Path(__file__).resolve().parents[2] / "scripts/check_local.py",
)
assert spec is not None and spec.loader is not None
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.mark.parametrize("url", [
    "https://example.com", "http://127.0.0.1.example.com", "http://user:secret@127.0.0.1",
    "http://127.0.0.1/path", "http://127.0.0.1?token=secret", "http://[::1",
])
def test_local_check_rejects_remote_or_ambiguous_origins(url):
    with pytest.raises(check.CheckError):
        check.loopback_url(url)


@pytest.mark.parametrize("model,research,external", [
    ("openrouter", "fixture", True), ("fixture", "treg", True), ("fixture", "fixture", True),
])
@pytest.mark.asyncio
async def test_local_check_never_generates_with_a_live_or_mixed_profile(model, research, external):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "mode": "local", "csrf_token": "synthetic-token", "model_provider": model,
            "research_provider": research, "external_calls_enabled": external,
        })

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond),
    ) as client:
        with pytest.raises(check.CheckError, match="both fixture"):
            await check.check_local(client, runs=1)
    assert "/api/v1/runs" not in paths
    assert paths == ["/ready", "/api/v1/session", "/api/v1/session"]


@pytest.mark.asyncio
async def test_local_check_validates_hash_replay_and_removes_only_its_own_run():
    content = b"> Synthetic development draft.\n\nExample text."
    deleted = []

    def respond(request):
        path = request.url.path
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(204)
        if path == "/api/v1/session":
            return httpx.Response(200, json={
                "mode": "local", "csrf_token": "synthetic-token", "model_provider": "fixture",
                "research_provider": "fixture", "external_calls_enabled": False,
            })
        if path == "/api/v1/runs":
            return httpx.Response(202, json={"id": "own-run"})
        if path.endswith("/download"):
            return httpx.Response(200, content=content)
        if path.endswith("/article"):
            return httpx.Response(200, json={"id": "revision", "content_hash": hashlib.sha256(content).hexdigest()})
        if path.endswith("/events"):
            if "last-event-id" in request.headers:
                return httpx.Response(204)
            return httpx.Response(200, text='id: 1\nevent: completed\ndata: {}\n\n')
        return httpx.Response(200, json={"state": "needs_review"})

    async with httpx.AsyncClient(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond),
    ) as client:
        report = await check.check_local(client, runs=1)
    assert report["completed"] == report["deleted"] == 1
    assert deleted == ["/api/v1/runs/own-run", "/api/v1/session"]
