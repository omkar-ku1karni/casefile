# Casefile: every target runs on a laptop with no keys (ORACLE=1) or with API keys.
SHELL := /bin/bash
CF := uv run casefile
ORACLE ?= 0
O := $(if $(filter 1,$(ORACLE)),--oracle,)
BATCH ?= test-v1
LOW_USD ?= 0.004

.PHONY: setup data db test lint doctor demo serve dev-eval test-eval low-ceiling report export replay-demo resume-demo graph clean-runs

setup:        ## install deps and create .env
	uv sync
	@test -f .env || cp .env.example .env

data:         ## regenerate the synthetic world, claim PDFs and eval set (deterministic)
	$(CF) build-data

db:           ## start embedded Postgres, create schema, seed the claims system
	$(CF) db init

test:         ## unit + property + end-to-end tests (oracle LLM, no keys)
	uv run pytest -q

lint:
	uv run ruff check src tests

doctor:       ## check keys, model names, Langfuse, database
	$(CF) doctor

demo: db      ## one claim end to end with the oracle LLM, approved at the gate
	$(CF) run CLM-0012 --oracle --approve --batch demo

serve: db     ## adjuster UI at http://127.0.0.1:8000  (ORACLE=1 for no keys)
	$(CF) serve $(O)

dev-eval:     ## iterate on the dev split only
	$(CF) batch dev-latest --split dev -s casefile $(O)

test-eval:    ## the frozen test split, all four systems
	$(CF) batch $(BATCH) --split test -s casefile -s casefile_no_reviewer -s single_agent -s single_prompt $(O)

low-ceiling:  ## same test claims, Casefile only, ceiling lowered to LOW_USD
	$(CF) batch $(BATCH)-lowceiling --split test -s casefile --max-usd $(LOW_USD) $(O)

report:       ## score a batch: eval/results/*.json + charts (no API calls)
	$(CF) eval $(BATCH) --low-ceiling-batch $(BATCH)-lowceiling

export:       ## ship-gate artifacts: 30 recorded runs to runs/recorded/
	$(CF) export $(BATCH) --n 30

replay-demo:  ## replay RUN=<run_id> from its step-2 snapshot using recorded LLM responses
	$(CF) replay $(RUN) --from-step 2

resume-demo:  ## kill a run just before `recommend`, then resume it from the Postgres checkpoint
	-CASEFILE_CRASH_BEFORE=recommend $(CF) run CLM-0003 --batch resume-demo $(O)
	$(CF) resume-latest resume-demo $(O)

graph:        ## print the LangGraph as Mermaid
	$(CF) graph

clean-runs:   ## delete all runs and checkpoints (keeps the claims system)
	$(CF) db reset-runs --yes
