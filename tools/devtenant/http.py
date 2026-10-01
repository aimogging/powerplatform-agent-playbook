"""One HTTP choke point with retry/backoff, JSON helpers and a replaceable transport (for offline tests).

Retry contract (ported from the proven deploy clients):
  * transport failures with NO response (DNS, refused, reset, timeout) retry with exponential backoff + jitter;
  * HTTP 408/429/500/502/503/504 retry; 429/503 honour Retry-After (seconds form), capped;
  * a WRITE (POST/PUT/PATCH) only retries when nothing can have been applied: a transport failure before any
    response, or 429/503 with Retry-After -- unless the caller passes allow_write_retry=True for an operation that is
    idempotent by nature (package import upsert-by-name, publish, read-only POSTs such as listWadl);
  * 400/401/403/404/409/412/422 are never retried here.
"""
import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, method, url, status, body):
        self.method, self.url, self.status, self.body = method, url, status, body
        short = url.split('?')[0]
        Exception.__init__(self, '%s %s -> HTTP %s: %s' % (method, short, status, (body or '')[:800]))


class Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body if isinstance(body, bytes) else (body or '').encode('utf-8')
        # runtime-package blobs are served gzip-encoded even without Accept-Encoding (live-observed); magic-byte check
        if self.body[:2] == bytes((0x1f, 0x8b)):
            try:
                self.body = gzip.decompress(self.body)
            except (OSError, EOFError):
                pass

    @property
    def text(self):
        return self.body.decode('utf-8-sig', errors='replace')

    def json(self):
        t = self.text
        return json.loads(t) if t.strip() else None


def safe_url(url):
    """Percent-encode characters urllib refuses (spaces in OData $filter etc.) without touching existing escapes."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


class UrllibTransport(object):
    def send(self, method, url, headers, data, timeout):
        req = urllib.request.Request(safe_url(url), data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers or {}), e.read() if hasattr(e, 'read') else b'')


class Http(object):
    def __init__(self, transport=None, max_attempts=5, base_delay=2, max_delay=30, sleep=time.sleep, log=print):
        self.transport = transport or UrllibTransport()
        self.max_attempts, self.base_delay, self.max_delay = max_attempts, base_delay, max_delay
        self.sleep, self.log = sleep, log

    def _delay(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        exp = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return random.uniform(max(1, exp / 2.0), exp)

    def request(self, method, url, headers=None, body=None, raw_body=None, timeout=180, allow_write_retry=False):
        hdrs = dict(headers or {})
        data = None
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body, separators=(',', ':')).encode('utf-8')
            hdrs.setdefault('Content-Type', 'application/json; charset=utf-8')
        is_write = method in ('POST', 'PUT', 'PATCH')
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.transport.send(method, url, hdrs, data, timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as ex:
                if attempt < self.max_attempts:
                    d = self._delay(attempt)
                    self.log('RETRY %d/%d %s %s after %s -- waiting %.0fs' % (attempt, self.max_attempts, method, url.split('?')[0], ex, d))
                    self.sleep(d)
                    continue
                raise
            if resp.status in RETRY_STATUS and attempt < self.max_attempts:
                ra = resp.headers.get('retry-after')
                ra = int(ra) if ra and ra.isdigit() else None
                safe = (not is_write) or allow_write_retry or (resp.status in (429, 503) and ra is not None)
                if safe:
                    d = self._delay(attempt, ra)
                    self.log('RETRY %d/%d %s %s after HTTP %d -- waiting %.0fs' % (attempt, self.max_attempts, method, url.split('?')[0], resp.status, d))
                    self.sleep(d)
                    continue
                self.log('WARN %s %s -> HTTP %d after a write; not retrying (it may have partially applied)' % (method, url.split('?')[0], resp.status))
            return resp

    def json(self, method, url, headers=None, body=None, ok=(200, 201, 202, 204), **kw):
        r = self.request(method, url, headers=headers, body=body, **kw)
        if r.status not in ok:
            raise HttpError(method, url, r.status, r.text)
        return r.json() if r.body else None


class FakeTransport(object):
    """Offline transport: routes = list of (method, url-substring, status, body-or-callable[, headers])."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def send(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        for route in self.routes:
            m, frag, status, body = route[:4]
            hdrs = route[4] if len(route) > 4 else {}
            if m == method and frag in url:
                if callable(body):
                    body = body(method, url, headers, data)
                if isinstance(body, (dict, list)):
                    body = json.dumps(body)
                return Response(status, hdrs, body if body is not None else '')
        return Response(404, {}, '{"error":{"code":"NoFakeRoute","message":"%s %s"}}' % (method, url))
