from __future__ import annotations

import hashlib
import time
from functools import reduce
from typing import Any
from urllib.parse import urlencode

import httpx


MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


class WbiSigner:
    def __init__(self) -> None:
        self.mixin_key: str | None = None
        self.last_update = 0.0

    def _get_mixin_key(self, source: str) -> str:
        return reduce(lambda acc, index: acc + source[index], MIXIN_KEY_ENC_TAB, "")[:32]

    async def _fetch_keys(self, cookies: dict[str, str] | None = None) -> None:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/",
                },
                cookies=cookies,
            )
            data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 WBI key 失败: {data.get('message') or data}")
        wbi_img = data["data"]["wbi_img"]
        img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]
        self.mixin_key = self._get_mixin_key(img_key + sub_key)
        self.last_update = time.time()

    async def sign(self, params: dict[str, Any], cookies: dict[str, str] | None = None) -> dict[str, Any]:
        if cookies or self.mixin_key is None or time.time() - self.last_update > 3600:
            await self._fetch_keys(cookies)
        clean = {key: "".join(char for char in str(value) if char not in "!'()*") for key, value in params.items()}
        clean["wts"] = int(time.time())
        clean = dict(sorted(clean.items()))
        query = urlencode(clean)
        clean["w_rid"] = hashlib.md5((query + (self.mixin_key or "")).encode()).hexdigest()
        return clean


wbi_signer = WbiSigner()
