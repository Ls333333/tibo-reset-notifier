from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

USER_AGENT = "tibo-watcher/1.0 (+personal push alerts for Codex limit news)"


def _open(request: urllib.request.Request, timeout: int) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_get(url: str, *, timeout: int = 25, retries: int = 2,
             headers: dict | None = None) -> str:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            return _open(request, timeout).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries + 1} tries: {url} ({last_error})")


def http_post(url: str, payload: bytes, *, timeout: int = 20,
              headers: dict | None = None) -> tuple[int, str]:
    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, "network error"


def get_json(url: str, **kwargs) -> dict:
    return json.loads(http_get(url, **kwargs))
