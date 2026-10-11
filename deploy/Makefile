# Superset deployment helpers.  Run from the deploy/ directory.
#
#   make up        build + start the stack
#   make smoke     run the post-deploy checks
#   make backup    dump metadata + archive the superset_home volume

COMPOSE ?= docker compose
BASE_URL ?= http://localhost:8088
PYTHON ?= python3
BACKUP_DIR := backups
STAMP := $(shell date +%Y%m%d-%H%M%S)

.PHONY: help up down restart build logs ps init shell smoke backup restore psql climate-sync clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## Build (if needed) and start everything
	$(COMPOSE) up -d --build

down: ## Stop and remove containers (volumes are kept)
	$(COMPOSE) down

restart: ## Restart the web application
	$(COMPOSE) restart superset

build: ## Rebuild the local image
	$(COMPOSE) build

logs: ## Follow application logs
	$(COMPOSE) logs -f superset

ps: ## Show container status
	$(COMPOSE) ps

init: ## Re-run metadata migrations, admin user and permission sync
	$(COMPOSE) run --rm superset-init

shell: ## Shell inside the running web container
	$(COMPOSE) exec superset bash

psql: ## psql into the DATA database (POSTGRES_DB)
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-superset} -d $${POSTGRES_DB:-superset}

psql-meta: ## psql into the METADATA database (superset_metadata)
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-superset} -d superset_metadata

smoke: ## Run the post-deploy checks against $(BASE_URL)
	./smoke_test.py --url $(BASE_URL)

climate-sync: ## Copy «Климат дома» data from the climate service into the data DB
	$(COMPOSE) cp tools/climate_sync.py superset:/tmp/climate_sync.py
	$(COMPOSE) exec -T superset /app/.venv/bin/python /tmp/climate_sync.py

backup: ## Dump metadata and archive superset_home into backups/
	@mkdir -p $(BACKUP_DIR)
	$(COMPOSE) exec -T db pg_dump -U $${POSTGRES_USER:-superset} superset_metadata \
	  > $(BACKUP_DIR)/superset_metadata_$(STAMP).sql
	docker run --rm -v superset_superset_home:/src:ro -v "$(PWD)/$(BACKUP_DIR)":/dst alpine \
	  sh -c 'cd /src && tar czf /dst/superset_home_$(STAMP).tgz .'
	@echo "written to $(BACKUP_DIR)/"

restore: ## Restore metadata from FILE=backups/superset_metadata_*.sql
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/superset_metadata_...sql"; exit 1; }
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-superset} -d superset_metadata < $(FILE)

clean: ## Stop everything AND delete volumes (destroys all Superset data)
	@printf 'This deletes the Postgres and superset_home volumes. Continue? [y/N] ' && read ans && [ "$$ans" = "y" ]
	$(COMPOSE) down -v
