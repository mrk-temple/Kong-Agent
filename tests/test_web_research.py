import asyncio
import json
import socket

import httpx
import pytest
from pydantic import SecretStr

from kong.tools.defaults import default_registry
from kong.web.config import WebConfig, load_web_config
from kong.web.service import WebService
from kong.web.tools import WebFetch
from kong.web.transport import WebError, public_url, request_public
from kong.workspace import Workspace


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://[::1]", "http://localhost/a", "http://x.local", "file:///x", "https://user:pass@example.com", "https://@example.com", "http://example.com:8080", "http://169.254.169.254"])
def test_reject_nonpublic_urls(url):
    with pytest.raises(WebError):
        public_url(url)


def test_config_registration_and_no_plaintext(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[web]\nmode="live"\nprovider="tavily"\n', encoding="utf-8")
    monkeypatch.setenv("TAVILY_API_KEY", "not-a-real-key")
    config = load_web_config(path)
    assert config.status()["search_available"]
    assert "not-a-real-key" not in config.model_dump_json()
    assert "web_search" in default_registry(Workspace(tmp_path), web_config=config).names()
    assert "web_fetch" not in default_registry(Workspace(tmp_path)).names()
    path.write_text('[web]\napi_key="secret"', encoding="utf-8")
    with pytest.raises(ValueError, match="api_key_env"):
        load_web_config(path)


def test_fetch_snapshot_pagination_cache_and_progress(tmp_path, monkeypatch):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(url)
        return 200, {"content-type":"text/html"}, b'<title>Test</title><script>evil()</script><p>Evidence text</p>', 'utf-8'
    monkeypatch.setattr("kong.web.service.request_public", request)
    config = WebConfig(mode="live")
    service = WebService(tmp_path, config)
    async def scenario():
        first = await service.fetch("https://example.com", max_chars=4)
        assert first["truncated"] and first["kind"] == "page"
        whole = service.read(first["source_id"])
        assert "Evidence text" in whole["content"] and "evil" not in whole["content"]
        again = await service.fetch("https://example.com", max_chars=4)
        assert again["cache_hit"] and again["historical_only"]
        assert WebFetch(service).progress_output(first) == WebFetch(service).progress_output(again)
        assert len(calls) == 1
        config.mode = "cached"
        assert (await service.fetch("https://example.com"))["cache_hit"]
        with pytest.raises(WebError, match="never falls back"):
            await service.fetch("https://example.org")
        with pytest.raises(WebError):
            await service.fetch("https://example.com", refresh=True)
        assert len(calls) == 1
    asyncio.run(scenario())


def test_search_provider_mapping_and_failed_response_not_cached(tmp_path, monkeypatch):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return 429, {}, b'secret-provider-error', 'utf-8'
        return 200, {}, json.dumps({"results":[{"title":"Official", "url":"https://docs.python.org/3/", "content":"lead"}]}).encode(), 'utf-8'
    monkeypatch.setattr("kong.web.service.request_public", request)
    service = WebService(tmp_path, WebConfig(mode="live", provider="tavily", api_key=SecretStr("private-key")))
    async def scenario():
        with pytest.raises(WebError, match="429") as exc:
            await service.search("Python", domains=["docs.python.org"], freshness="week")
        assert "secret" not in str(exc.value)
        result = await service.search("Python", domains=["docs.python.org"], freshness="week")
        assert result["results"][0]["kind"] == "search_snippet"
        assert "private-key" not in json.dumps(result)
        assert calls[1]["json_body"]["time_range"] == "week"
        assert calls[1]["json_body"]["include_domains"] == ["docs.python.org"]
        assert (await service.search("Python", domains=["docs.python.org"], freshness="week"))["cache_hit"]
        assert len(calls) == 2
    asyncio.run(scenario())


def test_redirect_to_private_never_requested(tmp_path, monkeypatch):
    calls = []
    async def request(method, url, **kwargs):
        calls.append(url)
        return 302, {"location":"http://127.0.0.1/secret"}, b'', 'utf-8'
    monkeypatch.setattr("kong.web.service.request_public", request)
    with pytest.raises(WebError):
        asyncio.run(WebService(tmp_path, WebConfig(mode="live")).fetch("https://example.com"))
    assert len(calls) == 1


@pytest.mark.parametrize("ip,proxy,allowed", [("198.18.0.2",None,False), ("198.18.0.2","http://127.0.0.1:7890",True), ("10.0.0.1","http://127.0.0.1:7890",False), ("93.184.216.34",None,True)])
def test_transport_dns_policy_and_original_tls_host(monkeypatch, ip, proxy, allowed):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a,**kw: [(socket.AF_INET,socket.SOCK_STREAM,6,"",(ip,443))])
    original = httpx.AsyncClient
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, stream=BytesStream(b'hello'))
    monkeypatch.setattr("kong.web.transport.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    async def scenario():
        operation = request_public("GET","https://example.com/x",timeout=3,max_bytes=100,proxy_url=proxy)
        if not allowed:
            with pytest.raises(WebError):
                await operation
            assert not captured
        else:
            result = await operation
            assert result[2] == b'hello'
            assert captured[0].headers["host"] == "example.com"
            assert captured[0].extensions["sni_hostname"] == "example.com"
            assert captured[0].url.host == ("example.com" if proxy else ip)
    asyncio.run(scenario())


def test_transport_response_limit(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a,**kw: [(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))])
    original = httpx.AsyncClient
    monkeypatch.setattr("kong.web.transport.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=BytesStream(b'12345')))))
    with pytest.raises(WebError, match="byte limit"):
        asyncio.run(request_public("GET","https://example.com",timeout=3,max_bytes=4))


def test_web_context_keeps_evidence_and_precise_resume_offset():
    from kong.continuity.compaction import web_excerpt
    output = {"source_id":"a"*32,"url":"https://example.com", "links":["x"*1000]*30,
              "offset":100, "content":"Evidence\n中文"*1000, "truncated":False}
    excerpt = web_excerpt(output, 1000)
    parsed = json.loads(excerpt)
    assert len(excerpt) <= 1000 and parsed["content"].startswith("Evidence")
    assert parsed["next_offset"] == 100 + len(parsed["content"])
    assert parsed["truncated"] and "links" not in parsed
