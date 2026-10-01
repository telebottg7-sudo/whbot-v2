"""
WatchHentai provider – series + episode resolver + media downloader.

Resolver pipeline (do not break this contract):
1. Series page  → extract /videos/ episode URLs
2. Episode page → data-primary-player-url
3. Player page  → var whJwSources = [...]
4. Decode each source["file"] → actual media URL
5. download() follows redirects + validates video content
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests
from PIL import Image


class ProviderError(Exception):
    """Raised on any unrecoverable provider failure."""


class WatchHentai:
    def __init__(self, base_url: str | None = None):
        self.base = (base_url or os.getenv("WATCHHENTAI_BASE_URL", "https://watchhentai.net")).rstrip("/")
        self.ua = (
            "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
        )
        self._series_cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Referer": referer or self.base + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _get(self, url: str, referer: str | None = None) -> str:
        r = requests.get(url, headers=self._headers(referer), timeout=(20, 40))
        if not r.ok:
            raise ProviderError(f"HTTP {r.status_code} for {url}")
        return r.text

    def _absolute(self, url: str) -> str:
        return urljoin(self.base + "/", url)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean(value: str | None) -> str | None:
        if not value:
            return None
        value = re.sub(r"<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>", " ", value, flags=re.I)
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        value = re.sub(r"<[^>]+>", " ", value)
        value = html_lib.unescape(value)
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _meta(html: str, name: str) -> str | None:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(name)}["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            m = re.search(pattern, html, re.I)
            if m:
                return WatchHentai._clean(m.group(1))
        return None

    # ------------------------------------------------------------------
    # Core decoder (must stay in sync with site encoding)
    # ------------------------------------------------------------------
    @staticmethod
    def _decode(value: str) -> str:
        """Decode the obfuscated source['file'] value into a real media URL."""
        value = value.replace("-", "+").replace("_", "/")
        value += "=" * (-len(value) % 4)
        decoded = base64.b64decode(value)
        decoded = bytes(
            byte ^ ((13 + (i % 17)) & 255)
            for i, byte in enumerate(decoded)
        )[::-1]
        return base64.b64decode(decoded).decode("utf-8")

    def _sources(self, player_html: str) -> list[dict[str, str]]:
        m = re.search(r"var\s+whJwSources\s*=\s*(\[[\s\S]*?\]);", player_html)
        if not m:
            print("[resolver] whJwSources not found", flush=True)
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            print(f"[resolver] whJwSources JSON error: {exc}", flush=True)
            return []

        print(f"[resolver] whJwSources entries: {len(data)}", flush=True)
        out: list[dict[str, str]] = []
        for index, source in enumerate(data, 1):
            try:
                label = source.get("label", "Unknown")
                decoded_url = self._decode(source["file"])
                print(
                    f"[resolver] source #{index} label={label} "
                    f"type={source.get('type', 'video/mp4')} url={decoded_url[:180]}",
                    flush=True,
                )
                out.append({
                    "label": label,
                    "type": source.get("type", "video/mp4"),
                    "url": decoded_url,
                })
            except Exception as exc:
                print(f"[resolver] source #{index} decode failed: {exc}", flush=True)
        return out

    # ------------------------------------------------------------------
    # Episode / series extraction
    # ------------------------------------------------------------------
    def _episode_links(self, html: str) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for m in re.finditer(r'href=["\']([^"\']*/videos/[^"\']+)["\']', html, re.I):
            url = self._absolute(m.group(1)).split("#")[0].rstrip("/")
            if url == self.base + "/videos" or url in seen:
                continue
            seen.add(url)
            out.append({"provider": "watchhentai", "page_url": url})
        return out

    def _series_page(self, page: str) -> dict[str, Any]:
        url = self._absolute(page)
        html = self._get(url)

        title = self._meta(html, "og:title")
        if title:
            title = re.sub(r"\s*-\s*Watch Hentai.*$", "", title, flags=re.I).strip()
        if not title:
            m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
            title = self._clean(m.group(1)) if m else url

        thumb = self._meta(html, "og:image")
        episode_links = self._episode_links(html)
        total = len(episode_links) if episode_links else 0
        if not total:
            m = re.search(r"([0-9]+)\s+Episodes", html, re.I)
            total = int(m.group(1)) if m else 0

        return {
            "provider": "watchhentai",
            "series_url": url,
            "name": title,
            "thumbnail": self._absolute(thumb) if thumb else None,
            "total_episodes": total,
        }

    def get_series(self, series_url: str) -> dict[str, Any]:
        return self._series_page(series_url)

    def series_episodes(self, series_url: str) -> list[dict[str, str]]:
        series_url = series_url.rstrip("/")
        if series_url in self._series_cache:
            return self._series_cache[series_url]

        html = self._get(series_url, self.base + "/")
        links = self._episode_links(html)
        self._series_cache[series_url] = links
        return links

    # ------------------------------------------------------------------
    # Main public resolver
    # ------------------------------------------------------------------
    def get_episode(self, page_url: str, resolve_sources: bool = True) -> dict[str, Any]:
        print(f"[resolver] get_episode: {page_url}", flush=True)
        if not re.search(r"watchhentai\.net/videos/", page_url, re.I):
            raise ProviderError("Not a WatchHentai episode URL")

        html = self._get(page_url)

        title = self._meta(html, "og:title")
        if not title:
            m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)
            title = self._clean(m.group(1)) if m else page_url

        thumb = self._meta(html, "og:image")
        synopsis = self._meta(html, "og:description") or self._meta(html, "description")

        m = re.search(r'data-primary-player-url=["\']([^"\']+)', html, re.I)
        if not m:
            raise ProviderError("Primary player URL not found")

        player = self._absolute(m.group(1))
        print(f"[resolver] player URL: {player}", flush=True)
        player_html = self._get(player, page_url)
        sources = self._sources(player_html) if resolve_sources else []
        print(f"[resolver] resolved sources: {len(sources)}", flush=True)

        em = re.search(r"episode[-\s]+(\d+)", (title or "") + " " + page_url, re.I)

        return {
            "provider": "watchhentai",
            "title": title,
            "episode": int(em.group(1)) if em else None,
            "synopsis": synopsis,
            "thumbnail": self._absolute(thumb) if thumb else None,
            "page_url": page_url,
            "player_url": player,
            "sources": sources,
        }

    # ------------------------------------------------------------------
    # Download helpers
    # ------------------------------------------------------------------
    def download_thumbnail(self, url: str, output: str | Path) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        response = requests.get(url, headers=self._headers(), timeout=(20, 40))
        if not response.ok:
            raise ProviderError(f"Thumbnail HTTP {response.status_code}")

        raw = output.with_suffix(".source")
        raw.write_bytes(response.content)
        try:
            with Image.open(raw) as image:
                image = image.convert("RGB")
                image.thumbnail((320, 320), Image.Resampling.LANCZOS)
                image.save(output, "JPEG", quality=85, optimize=True)
        finally:
            raw.unlink(missing_ok=True)

        if output.stat().st_size >= 200 * 1024:
            with Image.open(output) as image:
                image.save(output, "JPEG", quality=70, optimize=True)

        if output.stat().st_size >= 200 * 1024:
            raise ProviderError("Thumbnail could not be reduced below Telegram's 200 KB limit")

        return output

    def download(
        self,
        url: str,
        output: str | Path,
        progress: Callable[[int, int, float], None] | None = None,
        referer: str | None = None,
    ) -> Path:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        print("[downloader] START", flush=True)
        print(f"[downloader] URL: {url[:300]}", flush=True)

        headers = self._headers(referer or self.base + "/")
        headers["Accept"] = "video/mp4,video/*;q=0.9,application/octet-stream;q=0.8,*/*;q=0.5"

        for attempt in range(1, 4):
            try:
                print(f"[downloader] attempt {attempt}/3", flush=True)
                with requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=(30, 180),
                    allow_redirects=True,
                ) as response:
                    code = response.status_code
                    print(f"[downloader] HTTP status: {code}", flush=True)
                    print(f"[downloader] final URL: {response.url}", flush=True)
                    print(f"[downloader] content-type: {response.headers.get('content-type', 'unknown')}", flush=True)

                    if code in {403, 408, 425, 429} or code >= 500:
                        if attempt < 3:
                            time.sleep(float(attempt * 2))
                            continue
                        raise ProviderError(f"Media HTTP {code}")

                    if not response.ok:
                        raise ProviderError(f"Media HTTP {code}")

                    content_type = response.headers.get("content-type", "").lower()
                    total = int(response.headers.get("content-length") or 0)
                    current = 0

                    try:
                        with output.open("wb") as file:
                            first_chunk = True
                            for chunk in response.iter_content(1024 * 1024):
                                if not chunk:
                                    continue

                                if first_chunk:
                                    first_chunk = False
                                    probe = chunk[:1024].lstrip().lower()
                                    looks_html = (
                                        probe.startswith(b"<html")
                                        or probe.startswith(b"<!doctype")
                                        or b"<html" in probe[:256]
                                    )
                                    looks_video_type = (
                                        "video/" in content_type
                                        or "octet-stream" in content_type
                                    )
                                    looks_mp4 = b"ftyp" in chunk[:1024]

                                    if looks_html:
                                        raise ProviderError("Media server returned HTML instead of video")
                                    if not looks_video_type and not looks_mp4:
                                        raise ProviderError(
                                            f"Media response is not a recognized video ({content_type or 'unknown'})"
                                        )

                                file.write(chunk)
                                current += len(chunk)
                                if progress:
                                    progress(current, total, started)
                    except Exception:
                        output.unlink(missing_ok=True)
                        raise

                    if current <= 0:
                        raise ProviderError("Media response was empty")

                    print(f"[downloader] SUCCESS bytes={current}", flush=True)
                    return output

            except requests.RequestException as exc:
                print(f"[downloader] request error attempt {attempt}: {exc}", flush=True)
                output.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(float(attempt * 2))
                    continue
                raise ProviderError(f"Media request failed: {exc}") from exc

        raise ProviderError("Media download failed after all attempts")
