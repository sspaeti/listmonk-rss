all:	help

help:		## output help for all targets
	@echo "Local run for creating campaigns using Linkmonk"
	@echo "see README.md for details"
	@echo 
	@awk 'BEGIN {FS = ":.*?## "}; \
		/^###/ {printf "\n\033[1;33m%s\033[0m\n", substr($$0, 5)}; \
		/^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

PLANTUML_DIAGRAMS=$(shell echo assets/C4/*.puml)
PLANTUML_DIAGRAMS_PNG=$(PLANTUML_DIAGRAMS:.puml=.png)

$(PLANTUML_DIAGRAMS_PNG): %.png: %.puml
	plantuml $<

create_campaign: 	## check for new items in your feed and create a campaign 
	uv run listmonk_rss.py

clean:		        ## delete latest state of items already seen
	rm last_update.json

github_workflow:   	## test github workflow using act
	act workflow_dispatch --secret-file .env --var-file .env --container-architecture linux/amd64 --artifact-server-path /tmp/artifacts


diagrams: $(PLANTUML_DIAGRAMS_PNG) ## Generate architecture diagrams
