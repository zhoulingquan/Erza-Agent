"""系统级截屏端点(WebUI 剪刀按钮的本地直达路径)。"""

from __future__ import annotations

import asyncio

from websockets.http11 import Response

from erza.channels.websocket.api.screenshot_api import capture_screen_png, screenshot_response

from .._http_router import RouteContext, router
from ._common import forbidden, service_unavailable


@router.route("/api/screenshot", methods={"GET"})
async def capture_screenshot(ctx: RouteContext) -> Response:
    """截取网关所在主机的全部显示器为一张 PNG。

    安全边界:
    - token 校验由 dispatch 层执行(非 public 路由必须携带 Bearer token);
    - 仅允许 localhost 连接:截取的内容是本机屏幕,对远程访问者既无意义
      又有泄露风险,返回 403。
    """
    if not ctx.deps.is_localhost_connection(ctx.connection):
        return forbidden("screenshot requires a localhost connection")
    png = await asyncio.to_thread(capture_screen_png)
    if not png:
        return service_unavailable("screen capture unavailable")
    return screenshot_response(png)
