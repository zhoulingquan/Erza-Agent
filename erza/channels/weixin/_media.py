"""WeChat channel media download mixin.

Splits ``_download_media_item`` out of ``channel.py``. The patchable
``get_media_dir`` and ``time`` globals are resolved at call time through
the ``channel`` module namespace so test monkeypatches keep working.
"""

from __future__ import annotations

import base64
import os
from urllib.parse import quote

from erza.channels.weixin import channel as _ch


class MediaMixin:
    async def _download_media_item(
        self,
        typed_item: dict,
        media_type: str,
        filename: str | None = None,
    ) -> str | None:
        """Download + AES-decrypt a media item. Returns local path or None."""
        try:
            media = typed_item.get("media") or {}
            encrypt_query_param = str(media.get("encrypt_query_param", "") or "")
            full_url = str(media.get("full_url", "") or "").strip()

            if not encrypt_query_param and not full_url:
                return None

            # Resolve AES key (media-download.ts:43-45, pic-decrypt.ts:40-52)
            # image_item.aeskey is a raw hex string (16 bytes as 32 hex chars).
            # media.aes_key is always base64-encoded.
            # For images, prefer image_item.aeskey; for others use media.aes_key.
            raw_aeskey_hex = typed_item.get("aeskey", "")
            media_aes_key_b64 = media.get("aes_key", "")

            aes_key_b64: str = ""
            if raw_aeskey_hex:
                # Convert hex → raw bytes → base64 (matches media-download.ts:43-44)
                aes_key_b64 = base64.b64encode(bytes.fromhex(raw_aeskey_hex)).decode()
            elif media_aes_key_b64:
                aes_key_b64 = media_aes_key_b64

            # Reference protocol behavior: VOICE/FILE/VIDEO require aes_key;
            # only IMAGE may be downloaded as plain bytes when key is missing.
            if media_type != "image" and not aes_key_b64:
                return None

            assert self._client is not None
            fallback_url = ""
            if encrypt_query_param:
                fallback_url = (
                    f"{self.config.cdn_base_url}/download"
                    f"?encrypted_query_param={quote(encrypt_query_param)}"
                )

            download_candidates: list[tuple[str, str]] = []
            if full_url:
                download_candidates.append(("full_url", full_url))
            if fallback_url and (not full_url or fallback_url != full_url):
                download_candidates.append(("encrypt_query_param", fallback_url))

            data = b""
            for idx, (download_source, cdn_url) in enumerate(download_candidates):
                try:
                    resp = await self._client.get(cdn_url)
                    resp.raise_for_status()
                    data = resp.content
                    break
                except Exception as e:
                    has_more_candidates = idx + 1 < len(download_candidates)
                    should_fallback = (
                        download_source == "full_url"
                        and has_more_candidates
                        and self._is_retryable_media_download_error(e)
                    )
                    if should_fallback:
                        self.logger.warning(
                            "media download failed via full_url, falling back to encrypt_query_param: type={} err={}",
                            media_type,
                            e,
                        )
                        continue
                    raise

            if aes_key_b64 and data:
                data = _ch._decrypt_aes_ecb(data, aes_key_b64)

            if not data:
                return None

            media_dir = _ch.get_media_dir("weixin")
            ext = _ch._ext_for_type(media_type)
            if not filename:
                ts = int(_ch.time.time())
                hash_seed = encrypt_query_param or full_url
                h = abs(hash(hash_seed)) % 100000
                filename = f"{media_type}_{ts}_{h}{ext}"
            safe_name = os.path.basename(filename)
            file_path = media_dir / safe_name
            file_path.write_bytes(data)
            return str(file_path)

        except Exception:
            self.logger.exception("Error downloading media")
            return None
