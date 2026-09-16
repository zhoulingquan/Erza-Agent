"""Unit tests for the static-file serving rules (no channel instance needed)."""

from __future__ import annotations

from pathlib import Path

from erza.channels.websocket.static.serve import serve_static


def _headers(resp) -> dict[str, str]:
    return {k.lower(): v for k, v in resp.headers.items()}


def test_serves_index_at_root(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html/>", encoding="utf-8")
    resp = serve_static(tmp_path, "/")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"<html/>"
    assert _headers(resp)["cache-control"] == "no-cache"


def test_serves_hashed_asset_with_immutable_cache(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc123.js").write_text("x=1", encoding="utf-8")
    resp = serve_static(tmp_path, "/assets/app-abc123.js")
    assert resp is not None
    assert resp.status_code == 200
    assert "javascript" in _headers(resp)["content-type"]
    assert _headers(resp)["cache-control"] == "public, max-age=31536000, immutable"


def test_spa_fallback_to_index(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html/>", encoding="utf-8")
    resp = serve_static(tmp_path, "/settings/models")
    assert resp is not None
    assert resp.status_code == 200
    assert resp.body == b"<html/>"


def test_missing_everything_returns_none(tmp_path: Path) -> None:
    assert serve_static(tmp_path, "/settings/models") is None


def test_path_traversal_rejected(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html/>", encoding="utf-8")
    resp = serve_static(tmp_path, "/../secret.txt")
    assert resp is not None
    assert resp.status_code == 403
