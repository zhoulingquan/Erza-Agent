"""Static-file serving for the websocket channel's bundled WebUI.

Pure function extracted from ``WebSocketChannel._serve_static`` so the
SPA resolution rules (traversal rejection, history-mode fallback,
cache headers) are unit-testable without a channel instance.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from loguru import logger
from websockets.http11 import Response

from erza.channels.websocket._http_routes import _http_error, _http_response


def serve_static(dist_path: Path, request_path: str) -> Response | None:
    """Resolve *request_path* against the built SPA directory.

    SPA fallback to index.html; returns ``None`` when neither the file
    nor the fallback exists (caller falls through to other handlers).
    """
    rel = request_path.lstrip("/")
    if not rel:
        rel = "index.html"
    # Reject path-traversal attempts; the resolve()/relative_to() check below
    # is the authoritative guard.
    if ".." in rel.split("/"):
        return _http_error(403, "Forbidden")
    candidate = (dist_path / rel).resolve()
    try:
        candidate.relative_to(dist_path)
    except ValueError:
        return _http_error(403, "Forbidden")
    if not candidate.is_file():
        # SPA history-mode fallback: unknown routes serve index.html so the
        # client-side router can render them.
        index = dist_path / "index.html"
        if index.is_file():
            candidate = index
        else:
            return None
    try:
        body = candidate.read_bytes()
    except OSError as e:
        logger.warning("static: failed to read {}: {}", candidate, e)
        return _http_error(500, "Internal Server Error")
    ctype, _ = mimetypes.guess_type(candidate.name)
    if ctype is None:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
        ctype = f"{ctype}; charset=utf-8"
    # Hash-named build assets are cache-friendly; index.html must stay fresh.
    if candidate.name == "index.html":
        cache = "no-cache"
    elif "/brand/" in request_path:
        cache = "no-cache"
    else:
        cache = "public, max-age=31536000, immutable"
    return _http_response(
        body,
        status=200,
        content_type=ctype,
        extra_headers=[("Cache-Control", cache)],
    )
