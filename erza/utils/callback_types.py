"""Re-export shim for :mod:`erza.contracts.callbacks`.

The progress callback protocol canonicalized in ``erza/contracts/callbacks.py``
(契约层); this module keeps old imports working.
"""

from __future__ import annotations

from erza.contracts.callbacks import ProgressCallback

__all__ = ["ProgressCallback"]
