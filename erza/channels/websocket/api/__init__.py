"""WebUI HTTP API implementation for the websocket channel.

These modules back the ``handlers/`` routes served by ``WebSocketChannel``
(settings / channels / cron / tools / media / transcript / workspaces …).
They were historically the top-level ``erza.webui`` package; they are owned
by this channel and live here so the whole WebUI surface — transport
(``channel.py``), routing (``handlers/``) and business logic (``api/``) —
resides in one place.
"""
