"""Append-only JSON Lines audit log for API requests."""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

logger = logging.getLogger("sandboxd.audit")
_LOCK = threading.Lock()


def record(request: Request, status: int, principal) -> None:
    path = os.environ.get("SANDBOXD_AUDIT_LOG", "audit.log")
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": principal.subject if principal is not None else "anonymous",
        "role": principal.role if principal is not None else "anonymous",
        "method": request.method,
        "path": request.url.path,
        "status": status,
    }
    try:
        with _LOCK:
            with Path(path).open("a", encoding="utf-8") as log:
                log.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        logger.exception("Unable to append API audit event to %s", path)
