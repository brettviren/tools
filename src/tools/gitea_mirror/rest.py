"""A tiny stdlib JSON REST client with Link-header pagination."""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class ApiError(Exception):
    """An HTTP error from a REST API."""

    def __init__(self, status: int, method: str, url: str, message: str):
        super().__init__(f"{method} {url}: HTTP {status}: {message}")
        self.status = status
        self.message = message


def _error_message(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode(errors="replace")[:200]
    if isinstance(data, dict):
        return str(data.get("message") or data)
    return str(data)


def next_link(link_header: str | None) -> str | None:
    """Return the rel="next" URL of a Link header, if any."""
    if not link_header:
        return None
    m = _NEXT.search(link_header)
    return m.group(1) if m else None


class Rest:
    """JSON over HTTP relative to a base URL."""

    def __init__(self, base: str, headers: dict, timeout: float = 60):
        self.base = base.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def url(self, path: str, params: dict | None = None) -> str:
        """Join PATH (or pass through an absolute URL) with query PARAMS."""
        url = path if path.startswith("http") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> tuple[object, dict]:
        """Perform a request and return (decoded JSON or None, response headers)."""
        url = self.url(path, params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        log.debug("%s %s", method, url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                rheaders = dict(resp.headers.items())
        except urllib.error.HTTPError as err:
            raise ApiError(err.code, method, url, _error_message(err.read())) from err
        return (json.loads(raw) if raw.strip() else None), rheaders

    def get(self, path: str, params: dict | None = None):
        """GET and return decoded JSON."""
        return self.request("GET", path, params)[0]

    def post(self, path: str, body: dict):
        """POST BODY and return decoded JSON."""
        return self.request("POST", path, body=body)[0]

    def delete(self, path: str) -> None:
        """DELETE a resource."""
        self.request("DELETE", path)

    def paginate(self, path: str, params: dict | None = None, page_size: int = 50,
                 size_key: str = "per_page"):
        """Yield items across all pages, following Link rel=next."""
        url = self.url(path, {**(params or {}), size_key: page_size})
        while url:
            items, headers = self.request("GET", url)
            if not items:
                return
            yield from items
            url = next_link(headers.get("Link") or headers.get("link"))
