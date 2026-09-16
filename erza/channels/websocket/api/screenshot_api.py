"""系统级截屏:WebUI 剪刀按钮的后端直达路径。

WebUI 与网关同机运行时(本地部署的默认形态),剪刀按钮优先请求
``/api/screenshot``:后端直接调用系统 API 截取全部显示器,前端拿到
PNG 后即可进入微信式的暗色遮罩框选,完全绕过浏览器 getDisplayMedia
的"选择共享内容"授权卡(该卡片是浏览器安全边界,网页侧无法跳过)。

本模块只负责纯截屏;连接来源限制(仅 localhost)由路由 handler 执行。
"""

from __future__ import annotations

import email.utils
import http

from websockets.datastructures import Headers
from websockets.http11 import Response


def capture_screen_png() -> bytes | None:
    """截取全部显示器(虚拟屏并集)并返回 PNG 字节。

    失败(mss 未安装 / 无头环境 / 无显示器)返回 ``None``,handler 回复
    503,前端自动回退到 getDisplayMedia 授权流程。
    """
    try:
        import mss
        import mss.tools
    except ImportError:
        return None
    try:
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            return mss.tools.to_png(shot.rgb, shot.size)
    except Exception:
        return None


def screenshot_response(png: bytes) -> Response:
    """构造 image/png 二进制响应(截图是一次性数据,禁止缓存)。"""
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(png))),
        ("Content-Type", "image/png"),
        ("Cache-Control", "no-store"),
    ]
    reason = http.HTTPStatus(200).phrase
    return Response(200, reason, Headers(headers), png)
