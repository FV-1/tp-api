"""Limites de sandboxd : quota de sandboxes et durée de vie appliquée.

Branché dans main.py par deux appels :
    limits.check_quota(SANDBOXES)          dans createSandbox, avant d'enregistrer la sandbox
    limits.start_reaper(SANDBOXES, LOCK)   au démarrage : détruit les sandboxes expirées
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import docker
from .errors import Problem

MAX_SANDBOXES = int(os.environ.get("SANDBOXD_MAX_SANDBOXES", "10"))
REAP_INTERVAL = float(os.environ.get("SANDBOXD_REAP_INTERVAL", "5"))
ACTIVE = {"pending", "ready"}

log = logging.getLogger("sandboxd.limits")


def check_quota(sandboxes: dict) -> None:
    """409 si le nombre de sandboxes actives atteint le quota (à appeler sous LOCK).

    409 et non 429 : un quota atteint n'est pas passager, un client ne doit pas réessayer.
    """
    active = sum(1 for sb in sandboxes.values() if sb["status"] in ACTIVE)
    if active >= MAX_SANDBOXES:
        raise Problem(409, "quota-exceeded",
                      f"{active} sandboxes actives, le quota est de {MAX_SANDBOXES} : détruisez-en une")


def expired(sb: dict, now: datetime) -> bool:
    return sb["created_at"] + timedelta(seconds=sb["ttl_seconds"]) <= now


def reap_once(sandboxes: dict, lock) -> list[str]:
    """Détruit les sandboxes dont la durée de vie est écoulée. Renvoie leurs identifiants."""
    now = datetime.now(timezone.utc)
    with lock:
        victims = [sb for sb in sandboxes.values() if expired(sb, now)]
        for sb in victims:  # retirées d'abord : plus aucune route ne peut les utiliser
            del sandboxes[sb["id"]]
    for sb in victims:
        if sb.get("container_id"):
            try:
                docker.remove_container(sb["container_id"])
            except Exception:  # Docker indisponible : le conteneur sera retrouvé par make clean
                log.exception("suppression du conteneur de %s impossible", sb["id"])
        log.info("sandbox %s expirée et détruite", sb["id"])
    return [sb["id"] for sb in victims]


def start_reaper(sandboxes: dict, lock) -> threading.Thread:
    """Lance, en tâche de fond, la destruction périodique des sandboxes expirées."""
    def loop():
        while True:
            time.sleep(REAP_INTERVAL)
            try:
                reap_once(sandboxes, lock)
            except Exception:
                log.exception("nettoyage des sandboxes expirées en échec")

    thread = threading.Thread(target=loop, name="sandboxd-reaper", daemon=True)
    thread.start()
    return thread
