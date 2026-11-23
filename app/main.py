"""sandboxd — squelette du TP4.

Le contrat (openapi.yaml) fait foi : FastAPI ne génère pas le sien.
Fournis : le client Docker (docker.py), les erreurs problem+json (errors.py),
le stockage en mémoire, la tâche de création en arrière-plan et getSandbox
pour exemple. À vous d'écrire les autres routes : voir le sujet, partie 2.
"""
import base64
import json
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool

from . import auth, docker
from .errors import Problem, install

app = FastAPI(title="sandboxd", openapi_url=None, docs_url=None, redoc_url=None)
install(app)
auth.install(app)

SANDBOXES: dict[str, dict] = {}  # id -> sandbox (en mémoire : perdue au redémarrage)
IDEMPOTENCY_KEYS: dict[str, tuple[dict, str]] = {}
LOCK = threading.RLock()

ImageName = Literal["python:3.12-slim", "node:22-slim", "alpine:3.20"]


class SandboxLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: float = Field(default=1, ge=0.1, le=2)
    memory_mb: int = Field(default=512, ge=64, le=1024)


class SandboxCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: ImageName
    ttl_seconds: int = Field(default=600, ge=60, le=3600)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)


class SandboxPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(ge=60, le=3600)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(d: datetime) -> str:
    return d.isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return "sbx-" + secrets.token_hex(4)


def public(sb: dict) -> dict:
    """La représentation JSON d'une sandbox, conforme au schéma Sandbox."""
    return {
        "id": sb["id"], "image": sb["image"], "status": sb["status"], "ttl_seconds": sb["ttl_seconds"],
        "limits": sb["limits"], "created_at": iso(sb["created_at"]),
        "expires_at": iso(sb["created_at"] + timedelta(seconds=sb["ttl_seconds"])),
    }


def get_or_404(sandbox_id: str) -> dict:
    with LOCK:
        sb = SANDBOXES.get(sandbox_id)
    if sb is None:
        raise Problem(404, "not-found", f"{sandbox_id} n'existe pas")
    return sb


def etag(sb: dict) -> str:
    return f'"{sb["version"]}"'


def page_token(cursor: dict) -> str:
    raw = json.dumps(cursor, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_page_token(token: str, status: str | None) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("status") != status
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("created_at"), str)
        ):
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, value["id"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise Problem(400, "invalid-page-token", "page_token est invalide pour ce filtre") from exc


async def read_json(request: Request, model: type[BaseModel], media_types: tuple[str, ...]) -> BaseModel:
    """Lit le corps : Content-Type refusé (415), JSON invalide (400), contenu invalide (422)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype not in media_types:
        raise Problem(415, "unsupported-media-type", f"attendu : {', '.join(media_types)}")
    try:
        data = json.loads(await request.body())
    except ValueError:
        raise Problem(400, "invalid-json", "le corps n'est pas du JSON valide")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise Problem(422, "validation", f"{'.'.join(map(str, first['loc'])) or 'corps'} : {first['msg']}")


def provision(sandbox_id: str) -> None:
    """Tâche de fond : crée le conteneur, puis passe la sandbox en ready ou failed.

    À lancer depuis createSandbox, après avoir répondu 202 :
        threading.Thread(target=provision, args=(sandbox_id,), daemon=True).start()
    """
    with LOCK:
        sb = SANDBOXES.get(sandbox_id)
        if sb is None:
            return
        image = sb["image"]
        cpus = sb["limits"]["cpus"]
        memory_mb = sb["limits"]["memory_mb"]
    try:
        cid = docker.create_container(image, cpus, memory_mb,
                                      {"sandboxd": "1", "sandboxd.id": sandbox_id})
        with LOCK:
            current = SANDBOXES.get(sandbox_id)
            if current is not None:
                current.update(container_id=cid, status="ready", version=current["version"] + 1)
        if current is None:
            docker.remove_container(cid)
    except docker.DockerError:
        with LOCK:
            current = SANDBOXES.get(sandbox_id)
            if current is not None:
                current.update(status="failed", version=current["version"] + 1)


# ---------- exemple fourni ----------

@app.get("/v1/sandboxes/{sandbox_id}")
def get_sandbox(sandbox_id: str):
    with LOCK:
        sb = get_or_404(sandbox_id)
        return JSONResponse(public(sb), headers={"ETag": etag(sb)})


@app.post("/v1/sandboxes/{sandbox_id}/tokens", status_code=201)
def create_agent_token(sandbox_id: str):
    with LOCK:
        get_or_404(sandbox_id)
    return auth.issue_agent_token(sandbox_id)


@app.post("/v1/sandboxes", status_code=202)
async def create_sandbox(request: Request):
    model = await read_json(request, SandboxCreate, ("application/json",))
    body = model.model_dump()
    key = request.headers.get("Idempotency-Key")
    if key is not None and not 1 <= len(key) <= 255:
        raise Problem(400, "invalid-idempotency-key", "Idempotency-Key doit contenir entre 1 et 255 caractères")

    with LOCK:
        if key is not None and key in IDEMPOTENCY_KEYS:
            previous_body, sandbox_id = IDEMPOTENCY_KEYS[key]
            if previous_body != body:
                raise Problem(409, "idempotency-key-reused",
                              "Idempotency-Key a déjà été utilisée avec un contenu différent")
            sb = SANDBOXES.get(sandbox_id)
            if sb is None:
                raise Problem(404, "not-found", f"{sandbox_id} n'existe plus")
            return JSONResponse(
                public(sb), status_code=202,
                headers={"Location": f"/v1/sandboxes/{sandbox_id}", "ETag": etag(sb)},
            )

        sandbox_id = new_id()
        while sandbox_id in SANDBOXES:
            sandbox_id = new_id()
        sb = {
            "id": sandbox_id,
            "image": body["image"],
            "status": "pending",
            "ttl_seconds": body["ttl_seconds"],
            "limits": body["limits"],
            "created_at": now(),
            "version": 1,
        }
        SANDBOXES[sandbox_id] = sb
        if key is not None:
            IDEMPOTENCY_KEYS[key] = (body, sandbox_id)
        response_body = public(sb)
        response_etag = etag(sb)

    threading.Thread(target=provision, args=(sandbox_id,), daemon=True).start()
    return JSONResponse(
        response_body,
        status_code=202,
        headers={"Location": f"/v1/sandboxes/{sandbox_id}", "ETag": response_etag},
    )


@app.get("/v1/sandboxes")
def list_sandboxes(request: Request):
    params = request.query_params
    status = params.get("status")
    if status is not None and status not in {"pending", "ready", "failed", "terminated"}:
        raise Problem(400, "invalid-status", "status doit être pending, ready, failed ou terminated")

    raw_page_size = params.get("page_size", "20")
    try:
        size = int(raw_page_size)
    except ValueError as exc:
        raise Problem(400, "invalid-page-size", "page_size doit être un entier entre 1 et 100") from exc
    if not 1 <= size <= 100:
        raise Problem(400, "invalid-page-size", "page_size doit être un entier entre 1 et 100")

    token = params.get("page_token")
    cursor = None
    if token is not None:
        if not token or len(token) > 4096:
            raise Problem(400, "invalid-page-token", "page_token est invalide")
        cursor = parse_page_token(token, status)

    with LOCK:
        entries = [
            sb for sb in SANDBOXES.values()
            if status is None or sb["status"] == status
        ]
        entries.sort(key=lambda sb: (sb["created_at"], sb["id"]))
        if cursor is not None:
            entries = [sb for sb in entries if (sb["created_at"], sb["id"]) > cursor]
        selected = entries[:size + 1]
        has_next = len(selected) > size
        items = selected[:size]
        result = {"sandboxes": [public(sb) for sb in items]}
        if has_next:
            last = items[-1]
            result["next_page_token"] = page_token({
                "created_at": iso(last["created_at"]),
                "id": last["id"],
                "status": status,
            })
    return result


@app.patch("/v1/sandboxes/{sandbox_id}")
async def update_sandbox(sandbox_id: str, request: Request):
    with LOCK:
        sb = get_or_404(sandbox_id)
        current_etag = etag(sb)
    # Le corps d'abord (415, 400, 422), les préconditions ensuite (428, 412) :
    # une requête mal formée est refusée comme telle, quel que soit son If-Match.
    model = await read_json(request, SandboxPatch, ("application/merge-patch+json",))
    if_match = request.headers.get("If-Match")
    if if_match is None:
        raise Problem(428, "precondition-required", "l'en-tête If-Match est obligatoire")
    candidates = [candidate.strip() for candidate in if_match.split(",")]
    if any(not candidate for candidate in candidates):
        raise Problem(400, "invalid-if-match", "If-Match ne peut pas contenir de valeur vide")
    if "*" not in candidates and current_etag not in candidates:
        raise Problem(412, "precondition-failed", "la sandbox a été modifiée depuis sa lecture")

    with LOCK:
        sb = get_or_404(sandbox_id)
        if etag(sb) != current_etag:
            raise Problem(412, "precondition-failed", "la sandbox a été modifiée depuis sa lecture")
        if model.ttl_seconds < sb["ttl_seconds"]:
            raise Problem(422, "validation", "ttl_seconds ne peut que prolonger la durée de vie")
        sb["ttl_seconds"] = model.ttl_seconds
        sb["version"] += 1
        return JSONResponse(public(sb), headers={"ETag": etag(sb)})


@app.delete("/v1/sandboxes/{sandbox_id}", status_code=204)
def delete_sandbox(sandbox_id: str):
    with LOCK:
        sb = get_or_404(sandbox_id)
        container_id = sb.get("container_id")
    if container_id is not None:
        try:
            docker.remove_container(container_id)
        except docker.DockerError as exc:
            raise Problem(503, "docker-unavailable", exc.message) from exc
    with LOCK:
        if SANDBOXES.get(sandbox_id) is sb:
            del SANDBOXES[sandbox_id]
    return None


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------- exec et fichiers ----------

MAX_FILE = 1024 * 1024  # 1 Mio, comme dans le contrat
# Même règle que le paramètre FilePath du contrat : relatif, sans segment ".." ni
# segment vide, sans caractère de contrôle.
SAFE_PATH = re.compile(r"(?!(.*/)?\.\.(/|$))[^\x00-\x1f/]{1,255}(/[^\x00-\x1f/]{1,255})*")

Arg = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class ExecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: list[Arg] = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(default=10, ge=1, le=60)


def get_ready(sandbox_id: str) -> str:
    """Renvoie l'id du conteneur : 404 si la sandbox n'existe pas, 409 si elle n'est pas prête."""
    with LOCK:
        sb = get_or_404(sandbox_id)
        if sb["status"] != "ready":
            raise Problem(409, "not-ready", f"{sandbox_id} est en état {sb['status']}")
        return sb["container_id"]


def check_path(path: str) -> None:
    """422 si le chemin sort de /workspace (absolu, segment ..) ou que Linux le refuserait."""
    if len(path) > 1024 or not SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
        raise Problem(422, "validation", f"path : {path[:100]!r} est refusé (relatif à /workspace, sans ..)")
    if any(len(part.encode()) > 255 for part in path.split("/")):
        raise Problem(422, "validation", "path : un nom dépasse 255 octets")


async def call_docker(fn, *args):
    """Appelle le client Docker sans bloquer l'API, et traduit ses erreurs."""
    try:
        return await run_in_threadpool(fn, *args)
    except docker.DockerError as exc:
        if "not a directory" in exc.message:  # ex. a/b alors que a est un fichier
            raise Problem(409, "not-a-directory", "un élément du chemin est un fichier, pas un dossier") from exc
        if exc.status in (404, 409):  # conteneur supprimé ou arrêté entre-temps
            raise Problem(409, "not-ready", f"le conteneur n'est plus disponible : {exc.message}") from exc
        if exc.status == 400:
            raise Problem(422, "validation", f"refusé par Docker : {exc.message}") from exc
        raise Problem(503, "docker-unavailable", exc.message) from exc
    except httpx.HTTPError as exc:
        raise Problem(503, "docker-unavailable", "le démon Docker ne répond pas") from exc


async def is_dir(cid: str, path: str) -> bool:
    result = await call_docker(docker.exec_command, cid, ["test", "-d", path], 5)
    return result["exit_code"] == 0


@app.post("/v1/sandboxes/{sandbox_id}/exec")
async def exec_command(sandbox_id: str, request: Request):
    cid = get_ready(sandbox_id)
    body = await read_json(request, ExecRequest, ("application/json",))
    return await call_docker(docker.exec_command, cid, body.cmd, body.timeout_seconds)


@app.get("/v1/sandboxes/{sandbox_id}/files")
async def read_file(sandbox_id: str, path: str):
    cid = get_ready(sandbox_id)
    check_path(path)
    data = await call_docker(docker.read_file, cid, path)
    if data is None:
        if await is_dir(cid, path):
            raise Problem(409, "is-a-directory", f"{path} est un dossier")
        raise Problem(404, "file-not-found", f"{path} n'existe pas dans /workspace")
    return Response(data, media_type="application/octet-stream")


@app.put("/v1/sandboxes/{sandbox_id}/files")
async def write_file(sandbox_id: str, path: str, request: Request):
    cid = get_ready(sandbox_id)
    check_path(path)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/octet-stream":
        raise Problem(415, "unsupported-media-type", "attendu : application/octet-stream")
    data = await request.body()
    if len(data) > MAX_FILE:
        raise Problem(413, "payload-too-large", "le fichier dépasse 1 Mio")
    if await is_dir(cid, path):
        raise Problem(409, "is-a-directory", f"{path} est un dossier")
    created = await call_docker(docker.write_file, cid, path, data)
    return Response(status_code=201 if created else 204)
