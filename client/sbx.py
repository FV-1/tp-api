"""sbx — le client en ligne de commande de sandboxd. Squelette du TP4.

    python -m client.sbx ls [--status ready]
    python -m client.sbx create --image alpine:3.20 [--wait]
    python -m client.sbx exec sbx-a1b2c3d4 -- ls -la
    python -m client.sbx rm sbx-a1b2c3d4

L'adresse de l'API vient de SANDBOXD_URL (par défaut http://localhost:8000).
La robustesse face à une API capricieuse est dans client/transport.py
(request() et ApiError) ; ce fichier contient les commandes.
"""
import argparse
import os
import sys
import time
import uuid

import httpx

from .transport import ApiError, request

API = os.environ.get("SANDBOXD_URL", "http://localhost:8000")
# Clé opérateur ou jeton d'agent, envoyé en Authorization: Bearer (jamais écrit dans le code)
TOKEN = os.environ.get("SANDBOXD_TOKEN") or os.environ.get("SANDBOXD_API_KEY")
WAIT_TIMEOUT = 120  # secondes au plus pour create --wait
EXEC_MARGIN = 10    # secondes accordées en plus du timeout de la commande


def fail(r: httpx.Response) -> ApiError:
    """Erreur définitive, lisible, à partir d'une réponse d'erreur (problem+json)."""
    try:
        body = r.json()
        detail = body.get("detail") or body.get("title") or ""
    except ValueError:
        detail = r.text[:200]
    return ApiError(f"{r.request.method} {r.request.url.path} : {r.status_code} {detail}".strip())


def cmd_ls(client: httpx.Client, args) -> None:
    """Suit next_page_token jusqu'au bout, une ligne par sandbox."""
    params = {"page_size": 100}
    if args.status:
        params["status"] = args.status
    while True:
        r = request(client, "GET", "/v1/sandboxes", params=params)
        if r.status_code != 200:
            raise fail(r)
        page = r.json()
        # "sandboxes" dans notre contrat, "items" dans l'API capricieuse du kit
        for sb in page.get("sandboxes", page.get("items", [])):
            print(f'{sb["id"]}\t{sb["status"]}\t{sb["image"]}')
        token = page.get("next_page_token")
        if not token:
            return
        params["page_token"] = token


def wait_ready(client: httpx.Client, location: str, sb: dict) -> dict:
    """Interroge Location jusqu'à ready (renvoie la sandbox) ou failed (ApiError)."""
    deadline = time.monotonic() + WAIT_TIMEOUT
    delay = 0.5
    while sb["status"] == "pending":
        if time.monotonic() > deadline:
            raise ApiError(f"{sb['id']} est toujours pending après {WAIT_TIMEOUT} s")
        time.sleep(delay)
        delay = min(delay * 2, 5)
        r = request(client, "GET", location)
        if r.status_code != 200:
            raise fail(r)
        sb = r.json()
    if sb["status"] != "ready":
        raise ApiError(f"{sb['id']} est en état {sb['status']}")
    return sb


def cmd_create(client: httpx.Client, args) -> None:
    """Création rejouable : une Idempotency-Key par création, réutilisée à chaque essai."""
    key = str(uuid.uuid4())
    r = request(client, "POST", "/v1/sandboxes", json={"image": args.image},
                headers={"Idempotency-Key": key})
    if r.status_code not in (201, 202):
        raise fail(r)
    sb = r.json()
    if args.wait:
        sb = wait_ready(client, r.headers.get("Location") or f"/v1/sandboxes/{sb['id']}", sb)
    print(sb["id"])


def cmd_exec(client: httpx.Client, args) -> int:
    """Affiche stdout et stderr séparément, et sort avec le code de la commande."""
    if not args.cmd:
        raise ApiError("aucune commande : sbx exec <id> -- <commande> [arguments…]")
    body = {"cmd": args.cmd}
    if args.timeout:
        body["timeout_seconds"] = args.timeout
    # Pas d'Idempotency-Key : une commande ne doit jamais être exécutée deux fois.
    r = request(client, "POST", f"/v1/sandboxes/{args.id}/exec", json=body,
                timeout=(args.timeout or 10) + EXEC_MARGIN)
    if r.status_code != 200:
        raise fail(r)
    result = r.json()
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    if result.get("truncated"):
        print("sbx: sortie tronquée (1 Mio au plus par flux)", file=sys.stderr)
    if result.get("timed_out"):
        print("sbx: commande tuée à l'expiration de son délai", file=sys.stderr)
        return 124  # comme timeout(1)
    return result["exit_code"]


def cmd_rm(client: httpx.Client, args) -> None:
    """DELETE est idempotent : un 404 veut dire que la sandbox n'existe déjà plus."""
    r = request(client, "DELETE", f"/v1/sandboxes/{args.id}")
    if r.status_code == 404:
        print(f"sbx: {args.id} n'existait déjà plus", file=sys.stderr)
    elif not r.is_success:
        raise fail(r)


def main() -> int:
    parser = argparse.ArgumentParser(prog="sbx")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ls"); p.add_argument("--status"); p.set_defaults(func=cmd_ls)
    p = sub.add_parser("create"); p.add_argument("--image", required=True); p.add_argument("--wait", action="store_true"); p.set_defaults(func=cmd_create)
    p = sub.add_parser("exec"); p.add_argument("--timeout", type=int); p.add_argument("id"); p.add_argument("cmd", nargs=argparse.REMAINDER); p.set_defaults(func=cmd_exec)
    p = sub.add_parser("rm"); p.add_argument("id"); p.set_defaults(func=cmd_rm)
    args = parser.parse_args()
    if getattr(args, "cmd", None) and args.cmd[:1] == ["--"]:
        args.cmd = args.cmd[1:]
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    with httpx.Client(base_url=API, headers=headers) as client:  # un seul client : connexions réutilisées
        try:
            return args.func(client, args) or 0
        except ApiError as exc:
            print(f"sbx: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
