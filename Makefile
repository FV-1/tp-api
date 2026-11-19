# sandboxd — commandes du TP. Prérequis : Node 18+ (npx), Python 3.11+, Docker.
SPEC          ?= openapi.yaml
PORT          ?= 8000
DOCKER_SOCKET ?= /var/run/docker.sock
VENV          ?= .venv

FLAKY_PORT    ?= 8001

.PHONY: help lint mock docs install run test-contract clean flaky

help:
	@echo "make lint           vérifier le contrat (Spectral)"
	@echo "make mock           simuler l'API à partir du contrat (Prism, port 4010)"
	@echo "make docs           générer la documentation HTML (docs.html)"
	@echo "make install        installer les dépendances Python"
	@echo "make run            lancer sandboxd (port $(PORT))"
	@echo "make test-contract  rejouer le contrat contre sandboxd (Schemathesis)"
	@echo "make clean          supprimer les conteneurs créés par sandboxd"
	@echo "make flaky          lancer l'API capricieuse du TP4 (port $(FLAKY_PORT))"

lint:
	npx -y @stoplight/spectral-cli@6 lint $(SPEC) --ruleset .spectral.yaml

mock:
	npx -y @stoplight/prism-cli@5 mock $(SPEC) -p 4010

docs:
	npx -y @redocly/cli@latest build-docs $(SPEC) -o docs.html

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements.txt

run:
	DOCKER_SOCKET=$(DOCKER_SOCKET) $(VENV)/bin/uvicorn app.main:app --reload --port $(PORT)

# positive_data_acceptance est exclu : il considère comme une faute les refus
# légitimes liés à l'état (jeton de page inconnu, If-Match périmé, clé réutilisée).
test-contract:
	$(VENV)/bin/schemathesis run $(SPEC) --url http://localhost:$(PORT) -n 20 -w 1 \
		--exclude-checks positive_data_acceptance

clean:
	docker ps -aq --filter label=sandboxd=1 | xargs -r docker rm -f

# API capricieuse du TP4 (training/flaky_api.py). Rejouer un scénario : FLAKY_SEED=42 make flaky
flaky:
	$(VENV)/bin/uvicorn training.flaky_api:app --port $(FLAKY_PORT)
