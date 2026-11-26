# sandboxd

Projet fil rouge du cours d'API REST (4ESGI) — Victor Falc'hun et Lea Debruyne.

sandboxd, c'est une API qui crée des conteneurs Docker jetables pour lancer du code dedans. On crée une sandbox, on exécute des commandes, on lit et on écrit des fichiers, puis on la supprime. Et `sbx`, c'est notre client en ligne de commande, qui continue de marcher même quand l'API plante.

La présentation du projet est dans [`docs/presentation.pdf`](docs/presentation.pdf).

## Ce qu'il faut

- WSL (Debian ou Ubuntu) si vous êtes sous Windows : tout se lance depuis un terminal WSL
- Python 3.11 ou plus, avec `python3-venv`
- Docker, et l'accès à son socket (avec Docker Desktop : activer l'intégration WSL)
- Node 22 ou plus, seulement pour `make lint`, `make mock` et `make docs`

## Lancer le projet

```bash
make install   # installe les dépendances Python dans .venv
make run       # lance l'API sur http://localhost:8000
```

Au premier `make run`, un fichier `.env` est créé avec une clé opérateur et un secret JWT aléatoires. Il n'est pas dans Git (le modèle, c'est `.env.example`). Pas besoin de le toucher.

Dans un deuxième terminal, on charge la clé avant de faire des requêtes :

```bash
set -a; . ./.env; set +a
curl localhost:8000/healthz
```

Le serveur met une dizaine de secondes à démarrer depuis `/mnt/c` : si on a `Connection refused`, on attend un peu.

## Le client sbx

```bash
sbx() { .venv/bin/python -m client.sbx "$@"; }    # un raccourci, à refaire dans chaque terminal

ID=$(sbx create --image alpine:3.20 --wait)       # crée une sandbox et attend qu'elle soit prête
sbx ls                                            # liste toutes les sandboxes
sbx exec $ID -- echo bonjour                      # lance une commande dedans
sbx rm $ID                                        # la supprime
```

`sbx` envoie tout seul la clé opérateur (`SANDBOXD_OPERATOR_KEY`). Avec `SANDBOXD_TOKEN`, il utilise un jeton d'agent à la place. `SANDBOXD_URL` change l'adresse de l'API (par défaut `http://localhost:8000`).

## Les routes, avec un exemple curl pour chacune

Toutes les routes `/v1` demandent une clé opérateur (`X-Operator-Key`) ou un jeton d'agent (`Authorization: Bearer`). Le détail est dans `openapi.yaml` : c'est le contrat, et c'est lui qui fait foi.

```bash
K="X-Operator-Key: $SANDBOXD_OPERATOR_KEY"
J="Content-Type: application/json"

# créer une sandbox (202 + Location, elle passe de pending à ready en fond)
curl -i -X POST localhost:8000/v1/sandboxes -H "$K" -H "$J" -d '{"image": "alpine:3.20"}'

# lister (paginé, filtre par état possible)
curl -s "localhost:8000/v1/sandboxes?status=ready&page_size=20" -H "$K"

# en voir une (renvoie aussi un ETag)
curl -i localhost:8000/v1/sandboxes/$ID -H "$K"

# prolonger sa durée de vie (JSON Merge Patch, avec l'ETag lu juste avant)
curl -X PATCH localhost:8000/v1/sandboxes/$ID -H "$K" -H 'If-Match: "2"' \
     -H 'Content-Type: application/merge-patch+json' -d '{"ttl_seconds": 1800}'

# lancer une commande (une liste d'arguments, jamais une chaîne de shell)
curl -s -X POST localhost:8000/v1/sandboxes/$ID/exec -H "$K" -H "$J" -d '{"cmd": ["ls", "-la"], "timeout_seconds": 10}'

# écrire puis lire un fichier dans /workspace
curl -X PUT "localhost:8000/v1/sandboxes/$ID/files?path=hello.txt" -H "$K" \
     -H 'Content-Type: application/octet-stream' --data-binary 'salut'
curl -s "localhost:8000/v1/sandboxes/$ID/files?path=hello.txt" -H "$K"

# donner un jeton d'agent (15 min, valable seulement pour cette sandbox)
curl -s -X POST localhost:8000/v1/sandboxes/$ID/tokens -H "$K"

# supprimer
curl -i -X DELETE localhost:8000/v1/sandboxes/$ID -H "$K"
```

## Les commandes make

| Commande | Ce qu'elle fait |
|---|---|
| `make install` | Installe les dépendances Python dans `.venv` |
| `make run` | Lance l'API sur le port 8000 (et crée `.env` s'il manque) |
| `make lint` | Vérifie le contrat avec Spectral |
| `make mock` | Simule l'API à partir du contrat avec Prism (port 4010) |
| `make docs` | Génère la doc HTML du contrat (`docs.html`) |
| `make test-contract` | Teste l'API contre le contrat avec Schemathesis (l'API doit tourner) |
| `make test-auth` | Tests de sécurité : authentification, puis durcissement (l'API doit tourner) |
| `make flaky` | Lance l'API « capricieuse » du TP4 sur le port 8001 |
| `make clean` | Supprime les conteneurs et les volumes créés par sandboxd |

## Ce qu'on a fait

- **Le contrat** (`openapi.yaml`) : toutes les routes, les erreurs en problem+json, la pagination, les images autorisées, l'authentification.
- **L'API** (`app/`) : création en fond, Idempotency-Key et ETag / If-Match en bonus, exec, fichiers limités à `/workspace`. `make test-contract` passe.
- **Le client** (`client/`) : `transport.py` réessaie ce qui peut l'être (429, 502, 503, 504, réseau) avec Retry-After ou un backoff, et `sbx.py` gère la pagination, `--wait` et les doublons. Les mesures sont dans `tp4.md`.
- **La sécurité** : clé opérateur et jetons d'agent JWT, journal d'audit, conteneurs durcis (pas de réseau, pas root, lecture seule, pas de capabilities, processus limités), quotas, sandboxes expirées détruites, liens symboliques refusés. Le détail est dans `ATTAQUE.md`.

## Qui a fait quoi

| | Victor | Lea |
|---|---|---|
| Contrat | exec, fichiers, erreurs, exemples | create, list, update, delete |
| API | exec, readFile, writeFile | create, list, delete, update (+ bonus) |
| Client | les commandes (`sbx.py`) | le transport (`transport.py`) |
| Sécurité | durcissement, quotas, expiration, liens symboliques | authentification, jetons d'agent, audit |

## Les fichiers

| Fichier | Contenu |
|---|---|
| `openapi.yaml` | Le contrat de l'API |
| `app/` | L'API (FastAPI) : `main.py`, `auth.py`, `audit.py`, `limits.py`, `docker.py`, `errors.py` |
| `client/` | Le client `sbx` |
| `training/` | L'API capricieuse du TP4 |
| `tests/` | Tests d'authentification et de durcissement |
| `tp3.md`, `tp4.md` | Les traces des TP (tests de contrat, client robuste) |
| `ATTAQUE.md` | Le rapport d'attaque |
| `docs/presentation.pdf` | La présentation |
