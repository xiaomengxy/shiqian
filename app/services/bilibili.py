from __future__ import annotations

import base64
import io
import urllib.parse
from typing import Any

import httpx
import qrcode

from app.services.wbi import wbi_signer


class BilibiliService:
    BASE_URL = "https://api.bilibili.com"
    PASSPORT_URL = "https://passport.bilibili.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, sessdata: str | None = None, bili_jct: str | None = None, dedeuserid: str | None = None) -> None:
        self.sessdata = sessdata
        self.bili_jct = bili_jct
        self.dedeuserid = dedeuserid
        self.client = httpx.AsyncClient(timeout=30.0, headers=self.HEADERS)

    async def close(self) -> None:
        await self.client.aclose()

    def cookies(self) -> dict[str, str]:
        values = {}
        if self.sessdata:
            values["SESSDATA"] = self.sessdata
        if self.bili_jct:
            values["bili_jct"] = self.bili_jct
        if self.dedeuserid:
            values["DedeUserID"] = self.dedeuserid
        return values

    async def generate_qrcode(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.PASSPORT_URL}/x/passport-login/web/qrcode/generate")
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"生成二维码失败: {data.get('message')}")
        url = data["data"]["url"]
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {
            "qrcode_key": data["data"]["qrcode_key"],
            "qrcode_url": url,
            "qrcode_image_base64": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
        }

    async def poll_qrcode_status(self, qrcode_key: str) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.PASSPORT_URL}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
        )
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"轮询二维码失败: {data.get('message')}")
        inner_code = data["data"]["code"]
        status, message = {
            86101: ("waiting", "等待扫码"),
            86090: ("scanned", "已扫码，等待确认"),
            86038: ("expired", "二维码已过期"),
            0: ("confirmed", "登录成功"),
        }.get(inner_code, ("unknown", data["data"].get("message") or "未知状态"))
        result: dict[str, Any] = {"status": status, "message": message}
        if status == "confirmed":
            cookies = {cookie.name: cookie.value for cookie in response.cookies.jar}
            url = data["data"].get("url") or ""
            if "SESSDATA=" in url:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                for key in ["SESSDATA", "bili_jct", "DedeUserID"]:
                    if key in parsed:
                        cookies[key] = parsed[key][0]
            result["cookies"] = cookies
            result["refresh_token"] = data["data"].get("refresh_token") or ""
        return result

    async def get_user_info(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.BASE_URL}/x/web-interface/nav", cookies=self.cookies())
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取用户信息失败: {data.get('message')}")
        return data["data"]

    async def get_user_favorites(self, mid: int | str | None = None) -> list[dict[str, Any]]:
        mid = mid or self.dedeuserid
        if not mid:
            raise RuntimeError("未指定 Bilibili 用户 ID")
        response = await self.client.get(
            f"{self.BASE_URL}/x/v3/fav/folder/created/list-all",
            params={"up_mid": mid},
            cookies=self.cookies(),
        )
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取收藏夹失败: {data.get('message')}")
        return data["data"].get("list") or []

    async def get_favorite_content(self, media_id: int, pn: int = 1, ps: int = 20) -> dict[str, Any]:
        response = await self.client.get(
            f"{self.BASE_URL}/x/v3/fav/resource/list",
            params={"media_id": media_id, "pn": pn, "ps": min(ps, 20), "platform": "web"},
            cookies=self.cookies(),
        )
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取收藏夹内容失败: {data.get('message')}")
        return {
            "info": data["data"].get("info") or {},
            "medias": data["data"].get("medias") or [],
            "has_more": bool(data["data"].get("has_more")),
        }

    async def get_video_info(self, bvid: str) -> dict[str, Any]:
        response = await self.client.get(f"{self.BASE_URL}/x/web-interface/view", params={"bvid": bvid}, cookies=self.cookies())
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取视频信息失败: {data.get('message')}")
        return data["data"]

    async def get_video_summary(self, bvid: str, cid: int, up_mid: int | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"bvid": bvid, "cid": cid}
        if up_mid:
            params["up_mid"] = up_mid
        try:
            signed = await wbi_signer.sign(params, cookies=self.cookies())
            response = await self.client.get(
                f"{self.BASE_URL}/x/web-interface/view/conclusion/get",
                params=signed,
                cookies=self.cookies(),
            )
            data = response.json()
        except Exception:
            return None
        if data.get("code") != 0:
            return None
        return data.get("data") or None

    async def get_player_info(self, bvid: str, cid: int, aid: int | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"bvid": bvid, "cid": cid}
        if aid:
            params["aid"] = aid
        for url, signed in [
            (f"{self.BASE_URL}/x/player/wbi/v2", True),
            (f"{self.BASE_URL}/x/player/v2", False),
        ]:
            try:
                request_params = await wbi_signer.sign(params, cookies=self.cookies()) if signed else params
                response = await self.client.get(url, params=request_params, cookies=self.cookies())
                data = response.json()
                if data.get("code") == 0:
                    return data.get("data") or {}
            except Exception:
                continue
        return None

    async def download_subtitle(self, subtitle_url: str) -> str:
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        response = await self.client.get(subtitle_url)
        data = response.json()
        return "\n".join(item.get("content", "") for item in data.get("body", []) if item.get("content"))
