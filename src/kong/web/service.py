from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import hashlib
import re
from urllib.parse import urljoin, urlsplit

import httpx

from kong.web.store import WebStore, stable_key
from kong.web.transport import WebError, public_url, request_public


def now():
    return datetime.now(timezone.utc).isoformat()


class PageText(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.parts, self.title_parts, self.links = url, [], [], []
        self.skip, self.in_title = 0, False
        self.published_hint = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "template", "svg", "iframe"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta" and values.get("property", values.get("name")) in {"article:published_time", "date", "datePublished"}:
            self.published_hint = str(values.get("content", ""))[:100]
        if not self.skip:
            if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
                self.parts.append("\n")
            if tag == "a" and values.get("href") and len(self.links) < 30:
                try:
                    target = public_url(urljoin(self.url, values["href"]))
                    if target not in self.links:
                        self.links.append(target)
                except WebError:
                    pass

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg", "iframe"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "tr", "section"} and not self.skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return "\n".join(line for line in (re.sub(r"[ \t\r]+", " ", p).strip()
                                        for p in "".join(self.parts).splitlines()) if line)


class WebService:
    def __init__(self, workspace, config):
        self.config = config
        self.store = WebStore(workspace)

    async def request(self, method, url, **kwargs):
        if self.config.mode != "live":
            raise WebError("Network is disabled in this web mode")
        return await request_public(method, url, timeout=self.config.timeout,
                                    max_bytes=self.config.max_response_bytes, proxy_url=self.config.proxy_url, **kwargs)

    def snapshot(self, *, url, title, text, kind, **metadata):
        source_id = stable_key({"url": url, "title": title, "text": text, "kind": kind, **metadata})
        return self.store.put("source:" + source_id, {"source_id": source_id, "url": url,
            "title": title[:500], "text": text, "kind": kind, "retrieved_at": now(),
            "content_sha256": hashlib.sha256(text.encode()).hexdigest(), **metadata}, immutable=True)

    def read(self, source_id, offset=0, max_chars=8000):
        if self.config.mode == "disabled":
            raise WebError("Web tools are disabled")
        source = self.store.source(source_id)
        return {**{k: v for k, v in source.items() if k != "text"},
                "content": source["text"][offset:offset + max_chars], "offset": offset,
                "size": len(source["text"]), "truncated": offset + max_chars < len(source["text"]),
                "historical_only": True, "untrusted": True}

    async def search(self, query, count=5, domains=None, freshness=None, refresh=False):
        if self.config.mode == "disabled":
            raise WebError("Web tools are disabled")
        provider = self.config.provider
        if provider == "none":
            raise WebError("Select brave or tavily in [web]; model credentials do not provide search")
        domains = domains or []
        query = " ".join(query.split())
        key = "query:" + stable_key([provider, query, count, sorted(domains), freshness])
        cached = self.store.get(key, None if self.config.mode == "cached" else self.config.cache_ttl_seconds)
        if cached and not refresh:
            return {**cached, "cache_hit": True, "historical_only": True}
        if self.config.mode != "live":
            raise WebError("No matching cached search; cached mode never falls back to network")
        secret = self.config.api_key.get_secret_value()
        if not secret:
            raise WebError(f"Search key missing: set {self.config.api_key_env or 'the configured search environment variable'}")
        if provider == "brave":
            actual_query = query + (" (" + " OR ".join("site:" + d for d in domains) + ")" if domains else "")
            params = {"q": actual_query, "count": count}
            if freshness:
                params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[freshness]
            url = str(httpx.URL("https://api.search.brave.com/res/v1/web/search", params=params))
            status, _, body, _ = await self.request("GET", url, headers={"X-Subscription-Token": secret, "Accept": "application/json"})
        else:
            payload = {"query": query, "max_results": count, "search_depth": "basic",
                       "include_answer": False, "include_raw_content": False, "include_domains": domains}
            if freshness:
                payload["time_range"] = freshness
            status, _, body, _ = await self.request("POST", "https://api.tavily.com/search",
                headers={"Authorization": "Bearer " + secret}, json_body=payload)
        if status != 200:
            raise WebError(f"Search provider returned HTTP {status}; no fallback or retry")
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or data.get("error") or data.get("success") is False:
                raise ValueError()
            rows = data.get("web", {}).get("results", []) if provider == "brave" else data.get("results", [])
            if not isinstance(rows, list):
                raise ValueError()
            results = []
            for row in rows[:count]:
                if not isinstance(row, dict):
                    continue
                try:
                    url = public_url(str(row.get("url", "")))
                except WebError:
                    continue
                host = urlsplit(url).hostname
                if domains and not any(host == d or host.endswith("." + d) for d in domains):
                    continue
                snippet = unescape(re.sub(r"<[^>]+>", "", str(row.get("description", row.get("content", "")))))[:2000]
                source = self.snapshot(url=url, title=str(row.get("title", url))[:500], text=snippet,
                    kind="search_snippet", provider=provider, published_hint=str(row.get("published_date", row.get("age", "")))[:100])
                results.append({k: v for k, v in source.items() if k != "text"} | {"snippet": snippet})
        except (ValueError, TypeError, AttributeError) as exc:
            if isinstance(exc, WebError):
                raise
            raise WebError("Invalid search provider response") from exc
        result = {"query": query, "provider": provider, "results": results, "retrieved_at": now(),
                  "domains": domains, "freshness": freshness, "untrusted": True,
                  "note": "Search snippets are leads, not fetched page evidence. Fetch important sources before citing claims."}
        self.store.put(key, result)
        return {**result, "cache_hit": False, "historical_only": False}

    async def fetch(self, url, max_chars=8000, refresh=False):
        if self.config.mode == "disabled":
            raise WebError("Web tools are disabled")
        requested = public_url(url)
        key = "page:" + stable_key(requested)
        cached = self.store.get(key, None if self.config.mode == "cached" else self.config.cache_ttl_seconds)
        if cached and not refresh:
            return {**self.read(cached["source_id"], max_chars=max_chars), "cache_hit": True}
        if self.config.mode != "live":
            raise WebError("No matching cached page; cached mode never falls back to network")
        current = requested
        redirects = []
        for _ in range(5):
            status, headers, body, encoding = await self.request("GET", current,
                headers={"Accept": "text/html,text/plain,application/xhtml+xml"})
            if status in {301, 302, 303, 307, 308}:
                if "location" not in headers:
                    raise WebError("Redirect lacks Location")
                destination = public_url(urljoin(current, headers["location"]))
                if current.startswith("https:") and destination.startswith("http:"):
                    raise WebError("HTTPS downgrade redirect rejected")
                redirects.append(current)
                current = destination
                continue
            if status != 200:
                raise WebError(f"Page returned HTTP {status}; login/paywall/challenge bypass is unsupported")
            mime = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if mime not in {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}:
                raise WebError("Unsupported page format; this fetcher supports HTML and plain text, not PDF or browser rendering")
            try:
                decoded = body.decode(encoding, errors="replace")
            except LookupError:
                decoded = body.decode("utf-8", errors="replace")
            title, links, published_hint = current, [], None
            if mime in {"text/html", "application/xhtml+xml"}:
                parser = PageText(current)
                parser.feed(decoded)
                text = parser.text()
                title = " ".join(parser.title_parts).strip()[:500] or current
                links, published_hint = parser.links, parser.published_hint
            else:
                text = decoded
            if not text.strip():
                raise WebError("No readable text; this page may require JavaScript or browser access")
            source = self.snapshot(url=current, title=title, text=text[:120000], kind="page",
                requested_url=requested, redirects=redirects, links=links, published_hint=published_hint,
                source_truncated=len(text) > 120000, extraction="basic_html_text" if mime in {"text/html", "application/xhtml+xml"} else "plain_text")
            self.store.put(key, {"source_id": source["source_id"]})
            return {**self.read(source["source_id"], max_chars=max_chars), "cache_hit": False,
                    "historical_only": False, "checked_at": now()}
        raise WebError("Too many redirects")
