# Rapport d'attaque

Binôme : Victor Falc'hun / Lea Debruyne

Une fiche par faille trouvée ou subie : reproduction, impact, correctif, test. Les tests automatiques sont dans `tests/test_durcissement.py` :

```bash
make run                                                       # terminal 1 (variables de .env.example exportées)
.venv/bin/python -m tests.test_durcissement --quota 10 --ttl   # terminal 2, avec SANDBOXD_OPERATOR_KEY
```

## Faille 1 — Sortie de `/workspace` par un lien symbolique (trouvée chez nous)

**Reproduction**

```bash
sbx exec $ID -- ln -s /etc /workspace/etc
curl -s "localhost:8000/v1/sandboxes/$ID/files?path=etc/passwd"
```

Le chemin `etc/passwd` passe la vérification du TP3 : il est relatif et ne contient pas de `..`. Pourtant, l'API d'archive de Docker suit le lien, et renvoie `/etc/passwd` du conteneur.

**Impact.** Un agent lit, et avant le durcissement écrit, n'importe quel fichier du conteneur hors de `/workspace`, en contournant la règle « fichiers de `/workspace` seulement » du contrat. Le lien reste confiné au système de fichiers du conteneur, pas à celui de l'hôte.

**Correctif.** `check_no_symlink` (`app/main.py`) vérifie chaque élément du chemin, depuis `/workspace` : si l'un d'eux est un lien symbolique, la réponse est 422. Si la vérification ne peut pas s'exécuter (sandbox saturée), l'accès est refusé (409) plutôt qu'accordé. La racine du conteneur est de plus en lecture seule : même sans ce correctif, une écriture par le lien échouerait.

**Test.** `tests/test_durcissement.py`, section « Liens symboliques » : lire `etc/passwd` ou `pw` à travers un lien donne 422, écrire `etc/pirate` donne 422, et une écriture normale reste acceptée.

**Limite connue.** La vérification et la lecture sont deux appels successifs : un agent qui crée le lien exactement entre les deux (course TOCTOU) pourrait encore passer. La racine en lecture seule limite alors l'impact à la lecture.

## Attaques bloquées par le durcissement

Chaque sandbox est créée avec : utilisateur 1000 (non root), réseau `none`, racine en lecture seule, `CapDrop: ALL`, `no-new-privileges`, `PidsLimit: 128`, pas de swap. Seuls `/workspace` (tmpfs de 100 Mo, propriété de l'utilisateur) et `/tmp` (tmpfs de 64 Mo) sont inscriptibles.

| Attaque | Commande | Résultat |
|---|---|---|
| Fork bomb | `sh -c 'f() { f \| f & }; f'` | « can't fork » : la bombe sature sa sandbox (128 processus), les autres sandboxes et l'hôte ne sont pas touchés, la sandbox se détruit normalement |
| Accès réseau | `wget http://1.1.1.1`, `urlopen("http://example.com")` | Échec : aucune interface hors loopback, pas de DNS |
| Écriture hors de `/workspace` | `touch /etc/pirate` | « Read-only file system » |
| Élévation de privilèges | `grep CapEff /proc/self/status` | `CapEff: 0000000000000000`, `NoNewPrivs: 1` |
| Remplir le disque | `dd if=/dev/zero of=/workspace/gros bs=1M count=200` | « No space left on device » à 100 Mo |
| Sandbox oubliée | `ttl_seconds: 60`, puis attendre | Détruite par le nettoyeur ; `GET` renvoie 404 |
| Trop de sandboxes | au-delà de `SANDBOXD_MAX_SANDBOXES` (10) | 409 `quota-exceeded` |

## Attaque croisée

À remplir pendant l'attaque croisée : uniquement contre l'API du binôme voisin, avec le jeton qu'il nous donne.

### Faille N — titre (trouvée chez … / subie)

**Reproduction.** Les commandes exactes.

**Impact.** Ce qu'un attaquant obtient.

**Correctif.** Ce qui a été changé, et où.

**Test.** Le test qui le prouve, et son résultat.
