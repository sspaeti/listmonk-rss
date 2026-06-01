all:	help

help:		## output help for all targets
	@echo "Local run for creating campaigns using Listmonk"
	@echo "see README.md for details"
	@echo 
	@awk 'BEGIN {FS = ":.*?## "}; \
		/^###/ {printf "\n\033[1;33m%s\033[0m\n", substr($$0, 5)}; \
		/^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)

PLANTUML_DIAGRAMS=$(shell echo assets/C4/*.puml)
PLANTUML_DIAGRAMS_PNG=$(PLANTUML_DIAGRAMS:.puml=.png)

install:
	uv sync -U

$(PLANTUML_DIAGRAMS_PNG): %.png: %.puml
	plantuml $<

create_campaign: 	## check for new items in your feed and create a campaign
	uv run listmonk_rss.py

dry_run: 	## check for new items in your feed and create a campaign
	uv run listmonk_rss.py --dry-run

github_workflow:   	## test github workflow using act
	act workflow_dispatch --secret-file .env --var-file .env --container-architecture linux/amd64 --artifact-server-path /tmp/artifacts


### Free-flow newsletter (newsletter.py)

# Path to the latest draft, used by newsletter-send / newsletter-dry. Override:
#   make newsletter-send DRAFT=drafts/newsletter-2026-06-01.md
DRAFT ?= $(shell ls -t drafts/newsletter-*.md 2>/dev/null | head -n1)

newsletter:   ## gather brain/books/bluesky updates into drafts/newsletter-<date>.md
	uv run python newsletter.py gather

newsletter-since:   ## gather with custom start date (usage: make newsletter-since SINCE=2026-04-01)
	uv run python newsletter.py gather --since $(SINCE)

newsletter-edit:   ## open the latest draft in $EDITOR
	@test -n "$(DRAFT)" || (echo "No draft found in drafts/" && exit 1)
	$${EDITOR:-vi} $(DRAFT)

newsletter-send:   ## push the latest (or DRAFT=...) draft to Listmonk as a scheduled campaign
	@test -n "$(DRAFT)" || (echo "No draft found. Run 'make newsletter' first." && exit 1)
	uv run python newsletter.py send $(DRAFT)

newsletter-dry:   ## same as newsletter-send but with a 10-year delay (does not advance .last_newsletter)
	@test -n "$(DRAFT)" || (echo "No draft found. Run 'make newsletter' first." && exit 1)
	uv run python newsletter.py send $(DRAFT) --dry-run

bsky-engagement:   ## ad-hoc: print your top Bluesky posts via DuckDB (since=YYYY-MM-DD, default last 30d)
	@SINCE=$${SINCE:-$$(date -d '30 days ago' +%Y-%m-%d)}; \
	echo "Top Bluesky posts since $$SINCE:"; \
	uv run python -c "from newsletter import gather_bluesky; from datetime import datetime; \
	[print(f\"{p['engagement']:>4}  💜{p['likes']:>3} 🔁{p['reposts']:>2} 💬{p['replies']:>2}  {p['text'][:90]}  {p['url']}\") \
	 for p in gather_bluesky(datetime.fromisoformat('$$SINCE'), top_n=20)]"


diagrams: $(PLANTUML_DIAGRAMS_PNG) ## Generate architecture diagrams
