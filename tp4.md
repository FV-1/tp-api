# TP4 — Client robuste `sbx`

Binôme : Victor Falc'hun (commandes, `client/sbx.py`) / Lea Debruyne (transport, `client/transport.py`)

Toutes les mesures sont faites contre l'API capricieuse avec `FLAKY_SEED=42`, pour rejouer exactement le même scénario avant et après.

## Constat de départ (client naïf du kit)

```text
$ sbx ls | wc -l
20
$ for i in $(seq 10); do sbx create --image alpine:3.20; done
→ 7 succès, 3 plantages (KeyError: 'id')
$ curl -s localhost:8001/_stats
{"requests":11,"fail_429":1,"fail_503":1,"slow":1,"lost":1,"created":8,"idempotent_replays":0,"sandboxes":508}
```

| Mesure | Résultat | Pourquoi |
|---|---|---|
| `sbx ls` | **20** lignes sur 500 | Seule la première page est lue : `next_page_token` est ignoré. |
| 10 × `create` | **7** succès, **3** plantages | Un 429, un 503 et un 502 sont lus comme des succès. `r.json()["id"]` échoue sur le corps problem+json, avec une trace Python au lieu d'un message. |
| Sandboxes créées | **8** pour 7 succès annoncés | Réponse perdue : la sandbox est créée, mais le client reçoit un 502 et croit à un échec. Sans `Idempotency-Key`, la relancer en créerait une deuxième. |
| Lenteur | 1 réponse lente | Le client n'a pas de timeout à lui : il attend autant que l'API le décide. |

## Ce que nous avons changé

**Transport (`request()`)**
- **Timeout :** chaque appel a un timeout (3 s en lecture, 2 s en connexion). `exec` reçoit le délai de la commande, plus une marge.
- **Ce qui est réessayé :** uniquement 429, 502, 503, 504 et les erreurs réseau. Jamais un 400, 404 ou 422.
- **Méthodes concernées :** GET, PUT et DELETE sont rejouées. Un POST ne l'est que s'il porte une `Idempotency-Key`.
- **Attente :** on respecte `Retry-After`. Sinon, backoff exponentiel (0,5 s, 1 s, 2 s…) multiplié par un hasard entre 0,5 et 1,5.
- **Abandon :** au bout de 5 essais, `ApiError` avec un message clair et un code de sortie 1.

**Commandes (`client/sbx.py`)**
- **`ls` :** suit `next_page_token` jusqu'au bout.
- **`create` :** une `Idempotency-Key` (UUID) par création, réutilisée à chaque nouvel essai. Avec `--wait`, on interroge `Location` jusqu'à `ready` (l'identifiant s'affiche alors) ou `failed` (erreur).
- **`exec` :**
  - stdout et stderr sont affichés séparément ;
  - le code de sortie de `sbx` est celui de la commande, ou 124 si elle a été tuée par son délai ;
  - pas d'`Idempotency-Key` : une commande ne doit jamais s'exécuter deux fois, donc un `exec` n'est jamais rejoué.
- **`rm` :** un 404 n'est pas une erreur, puisque `DELETE` est idempotent.
- **Erreurs :** toute réponse d'erreur devient un message lisible (`sbx: POST /v1/... : 404 sbx-… n'existe pas`), sans trace Python.
- **Client HTTP :** un seul `httpx.Client` par commande.

## Mêmes mesures avec notre client

```text
$ sbx ls | wc -l
500                       (0 doublon ; 8 nouveaux essais : 3 timeouts, 4 × 503, 1 × 429)
$ for i in $(seq 10); do sbx create --image alpine:3.20; done
→ 10 succès, 0 échec
$ curl -s localhost:8001/_stats
{"requests":53,"fail_429":4,"fail_503":7,"slow":6,"lost":1,"created":10,"idempotent_replays":1,"sandboxes":510}
```

| Mesure | Client naïf | Notre client |
|---|---|---|
| `sbx ls` (500 sandboxes) | 20 lignes | **500** lignes, 0 doublon |
| 10 × `create` | 7 succès, 3 plantages | **10** succès |
| Sandboxes en plus | 8 (incohérent) | **10** : la réponse perdue a été rejouée avec la même clé (`idempotent_replays: 1`) |

## Vérifications du sujet

| Vérification | Résultat |
|---|---|
| `sbx ls \| wc -l`, puis `sort \| uniq -d` | 500 lignes, aucun doublon |
| 20 × `sbx create` | 20 succès, exactement 20 sandboxes de plus dans `/_stats` |
| `sbx create --wait` | L'identifiant s'affiche une fois la sandbox `ready` |
| `sbx rm` deux fois de suite | Code de sortie 0 les deux fois (« n'existait déjà plus » au second) |
| Erreur définitive (exec sur une sandbox inconnue) | Pas de nouvel essai, message clair, code 1 |
| Contre notre sandboxd (`SANDBOXD_URL=http://localhost:8000`) | `create --wait`, `ls`, `ls --status ready`, `exec` (code 0, code 3 avec stderr séparé, timeout → 124), `rm` × 2 : tout fonctionne |

## Remarques

- **Champ de la liste :** l'API capricieuse renvoie la liste dans `items`, alors que notre contrat dit `sandboxes`. `sbx ls` accepte les deux.
- **Les `exec` en échec :** face à l'API capricieuse, un `exec` qui tombe sur un 429 ou un 503 échoue avec un message clair. C'est voulu : sans `Idempotency-Key`, un POST n'est pas rejoué.
- **La durée des commandes :** chaque commande `sbx` met environ 3,5 s à démarrer (lancement de Python depuis `/mnt/c` sous WSL). Ce temps compte dans les durées mesurées.
