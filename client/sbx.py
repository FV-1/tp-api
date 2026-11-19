"""sbx — le client en ligne de commande de sandboxd. Squelette du TP4.

    python -m client.sbx ls [--status ready]
    python -m client.sbx create --image alpine:3.20 [--wait]
    python -m client.sbx exec sbx-a1b2c3d4 -- ls -la
    python -m client.sbx rm sbx-a1b2c3d4

L'adresse de l'API vient de SANDBOXD_URL (par défaut http://localhost:8000).
L'affichage et la lecture des arguments sont fournis. À vous : la fonction
request(), qui doit tenir face à une API capricieuse, et les commandes.
"""
import argparse
import json
import os
import random
import sys
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx

API = os.environ.get("SANDBOXD_URL", "http://localhost:8000")


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


def cmd_ls(client: httpx.Client, args) -> None:
    """TODO TP4 : suivre next_page_token jusqu'au bout, une ligne par sandbox."""
    r = request(client, "GET", "/v1/sandboxes", params={"status": args.status} if args.status else None)
    for sb in r.json()["items"]:
        print(f'{sb["id"]}\t{sb["status"]}\t{sb["image"]}')


def cmd_create(client: httpx.Client, args) -> None:
    """TODO TP4 : création rejouable (Idempotency-Key), puis --wait jusqu'à ready ou failed."""
    r = request(client, "POST", "/v1/sandboxes", json={"image": args.image})
    print(r.json()["id"])


def cmd_exec(client: httpx.Client, args) -> None:
    """TODO TP4 : afficher stdout et stderr séparément, et sortir avec le code de la commande."""
    r = request(client, "POST", f"/v1/sandboxes/{args.id}/exec", json={"cmd": args.cmd})
    print(json.dumps(r.json(), indent=2))


def cmd_rm(client: httpx.Client, args) -> None:
    """TODO TP4 : un 404 au deuxième essai n'est pas une erreur (DELETE est idempotent)."""
    request(client, "DELETE", f"/v1/sandboxes/{args.id}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="sbx")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ls"); p.add_argument("--status"); p.set_defaults(func=cmd_ls)
    p = sub.add_parser("create"); p.add_argument("--image", required=True); p.add_argument("--wait", action="store_true"); p.set_defaults(func=cmd_create)
    p = sub.add_parser("exec"); p.add_argument("id"); p.add_argument("cmd", nargs=argparse.REMAINDER); p.set_defaults(func=cmd_exec)
    p = sub.add_parser("rm"); p.add_argument("id"); p.set_defaults(func=cmd_rm)
    args = parser.parse_args()
    if getattr(args, "cmd", None) and args.cmd[:1] == ["--"]:
        args.cmd = args.cmd[1:]
    with httpx.Client(base_url=API) as client:  # un seul client : connexions réutilisées
        try:
            return args.func(client, args) or 0
        except ApiError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
