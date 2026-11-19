# TP3 — Implémentation de sandboxd

Binôme : Victor Falc'hun / Lea Debruyne

## Dernière sortie de `make test-contract`

```text
.venv/bin/schemathesis run openapi.yaml --url http://localhost:8000 -n 20 -w 1 \
	--exclude-checks positive_data_acceptance
Schemathesis v4.32.7
━━━━━━━━━━━━━━━━━━━━

 ✅  Loaded specification from openapi.yaml (in 7.94s)

     Base URL:         http://localhost:8000
     Specification:    Open API 3.1.0
     Operations:       9 selected / 9 total

 ✅  API capabilities:

     Supports NULL byte in headers:                            ✘
     Accepts backslash and control characters in URL paths:    ✓

 ✅  Examples (in 1.43s)

     ✅ 7 passed  ⏭  2 skipped

 ✅  Coverage (in 10.42s)

     ✅ 9 passed

 ✅  Fuzzing (in 33.88s)

     ✅ 9 passed

 ✅  Stateful (in 14.70s)

     Scenarios:    20
     API Links:    15 covered / 23 selected / 23 total (23 inferred)

     ✅ 20 passed

=================================== WARNINGS ===================================

Schema validation mismatch: 1 operation mostly rejected generated data due to validation errors, indicating schema constraints don't match API validation

  - PATCH /v1/sandboxes/{sandbox_id}

💡 Check your schema constraints - API validation may be stricter than documented

=================================== SUMMARY ====================================

API Operations:
  Selected: 9/9
  Tested: 9

Test Phases:
  ✅ Examples
  ✅ Coverage
  ✅ Fuzzing
  ✅ Stateful

Warnings:
  ⚠️ Schema validation mismatch: 1 operation mostly rejected generated data

Test cases:
  577 generated, 577 passed

Seed: 249464200820409233557080180916811883355

============================= 1 warning in 61.03s ==============================
```

## Écarts code / contrat corrigés

| # | Échec Schemathesis | Cause | Décision |
|---|---|---|---|
| 1 | `PATCH` sans `If-Match` : 428 reçu, 400/422 attendu (ST203) | Le contrat déclarait `If-Match` en `required: true`. Schemathesis attend alors une erreur de validation quand il manque. | **Contrat corrigé.** On garde 428 Precondition Required (RFC 6585), qui est le bon code. `If-Match` n'est plus marqué `required` : c'est la réponse 428 qui décrit son absence. |
| 2 | `GET`/`PUT /files` avec un nom de 4096 caractères : 503 (ST101) | Linux limite un nom de fichier à 255 octets ; Docker renvoyait une erreur 500, traduite en 503. | **Code et contrat corrigés.** Chaque nom fait 255 octets au plus, le chemin 1024 caractères au plus. Au-delà : 422. Le `pattern` de `FilePath` l'exprime. |
| 3 | `PATCH` avec un corps invalide et un `If-Match` périmé : 412 (ST201) | Le code vérifiait la précondition avant le corps. | **Code corrigé.** Le corps est validé d'abord (415, 400, 422), les préconditions ensuite (428, 412). |
| 4 | `GET /files?path=0/0/00/0` alors que `0` est un fichier : 503 (ST101) | Docker répond « not a directory » (500). | **Code et contrat corrigés.** Un chemin qui passe par un fichier renvoie 409, comme un chemin qui désigne un dossier. |

En corrigeant le n° 2, nous avons aussi interdit les caractères de contrôle dans `path`. Avec un retour à la ligne, l'expression régulière ne voyait plus un `..` placé après.

## Avertissement restant (accepté)

`PATCH /v1/sandboxes/{sandbox_id}` rejette la plupart des requêtes générées. C'est voulu : sans `If-Match` valide, la réponse est 428 ou 412. Un `ttl_seconds` plus petit que l'actuel est refusé (422), car la modification ne peut que prolonger la durée de vie. Schemathesis ne connaît pas l'`ETag` courant et ne peut donc presque jamais réussir un `PATCH`.

## Tests manuels (sandbox `alpine:3.20` réelle)

| Cas | Résultat |
|---|---|
| Création | 202, `Location`, passage de `pending` à `ready` |
| Même `Idempotency-Key`, même corps / autre corps | même sandbox / 409 |
| Exec pendant `pending` | 409 |
| `["echo", "bonjour"]` | `exit_code: 0`, `stdout: "bonjour\n"` |
| `["sh", "-c", "echo oups >&2; exit 3"]` | `exit_code: 3`, `stderr: "oups\n"` |
| `["sleep", "10"]` avec `timeout_seconds: 2` | `timed_out: true`, `exit_code: null`, ~2,1 s |
| Exécutable introuvable | `exit_code: 127` |
| Écrire puis réécrire un fichier | 201 puis 204 ; relu à l'identique, visible par `cat` |
| Fichier absent / dossier / chemin qui passe par un fichier | 404 / 409 / 409 |
| `../etc/passwd`, `src/../../etc/passwd`, `/etc/passwd` | 422 |
| Corps de 1,1 Mo / Content-Type JSON | 413 / 415 |
| `DELETE` puis `DELETE` | 204 puis 404, conteneur supprimé |
