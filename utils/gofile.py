"""Simple GoFile uploader (server auto-selection + guest upload)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests


class GofileUploader:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GOFILE_TOKEN")
        self._server: str | None = None

    def _get_server(self) -> str:
        if self._server:
            return self._server
        r = requests.get("https://api.gofile.io/servers", timeout=15)
        r.raise_for_status()
        data = r.json()
        servers = data.get("data", {}).get("servers") or []
        if not servers:
            raise RuntimeError("No GoFile servers available")
        self._server = servers[0]["name"]
        return self._server

    def upload(self, file_path: str | Path, progress_callback=None) -> dict:
        """
        Upload a file and return {"downloadPage": "...", "fileId": "...", ...}
        """
        file_path = Path(file_path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)

        server = self._get_server()
        url = f"https://{server}.gofile.io/uploadFile"

        data = {}
        if self.token:
            data["token"] = self.token

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f)}
            r = requests.post(url, data=data, files=files, timeout=600)
            r.raise_for_status()
            result = r.json()

        if result.get("status") != "ok":
            raise RuntimeError(f"GoFile upload failed: {result}")

        return result["data"]
