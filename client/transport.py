"""Transport HTTP robuste de sbx (TP4, partie de Lea).

    request(client, method, path, **kwargs) -> httpx.Response
        Renvoie la réponse finale, y compris 4xx : c'est sbx.py qui l'interprète.
        kwargs est passé à httpx (params, json, headers, timeout…).
    ApiError : levée quand on abandonne (essais épuisés, API inaccessible).

Timeout sur chaque appel ; nouveaux essais seulement sur 429, 502, 503, 504 et
erreurs réseau, et seulement pour une requête idempotente ou qui porte une
Idempotency-Key ; Retry-After respecté, sinon backoff exponentiel avec jitter.
"""
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


class ApiError(Exception):
    """Erreur définitive : inutile de réessayer."""


RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"}
MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT_SECONDS = 5.0


def _retryable(method: str, kwargs: dict) -> bool:
    """Une requête peut être rejouée sans risquer une seconde création."""
    headers = kwargs.get("headers") or {}
    has_idempotency_key = any(
        str(name).lower() == "idempotency-key"
        for name in headers
    )
    return method.upper() in IDEMPOTENT_METHODS or has_idempotency_key


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _backoff(attempt: int) -> float:
    # attempt vaut 1 pour le premier nouveau essai.
    return min(8.0, 0.25 * (2 ** (attempt - 1))) * random.uniform(0.8, 1.2)


def request(client: httpx.Client, method: str, path: str, **kwargs) -> httpx.Response:
    """Envoie une requête avec timeout et retries sûrs."""
    method = method.upper()
    kwargs["timeout"] = kwargs.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    retryable_request = _retryable(method, kwargs)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            if not retryable_request or attempt == MAX_ATTEMPTS:
                raise ApiError(
                    f"API inaccessible après {attempt} tentative(s) "
                    f"pour {method} {path}: {exc}"
                ) from exc
            delay = _backoff(attempt)
            time.sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if not retryable_request or attempt == MAX_ATTEMPTS:
            if attempt == MAX_ATTEMPTS:
                raise ApiError(
                    f"API indisponible : HTTP {response.status_code} "
                    f"après {attempt} tentatives pour {method} {path}"
                )
            return response

        delay = _retry_after(response)
        if delay is None:
            delay = _backoff(attempt)
        time.sleep(delay)

    raise ApiError(f"API inaccessible après {MAX_ATTEMPTS} tentatives")
