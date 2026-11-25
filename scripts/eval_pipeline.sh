#!/usr/bin/env bash
# Unattended eval on free tiers. Every step is idempotent (finished claims are skipped); when a provider's
# daily quota runs out the batch stops itself, and this loop waits and resumes after the reset.
# Usage: nohup caffeinate -i scripts/eval_pipeline.sh > var/logs/pipeline.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
export CASEFILE_NO_FALLBACK=1 PYTHONUNBUFFERED=1
CF="uv run casefile"
SHARDS=${SHARDS:-2}

run_until_done() {  # $1 batch  $2 split  $3 system
  while [ "$($CF remaining "$1" --split "$2" --system "$3")" != "0" ]; do
    echo "$(date -u +%FT%TZ) $1 $3: $($CF remaining "$1" --split "$2" --system "$3") claims left"
    for ((i = 0; i < SHARDS; i++)); do
      $CF batch "$1" --split "$2" -s "$3" --shard "$i/$SHARDS" >> "var/logs/$1.$3.shard$i.log" 2>&1 &
    done
    wait
    left=$($CF remaining "$1" --split "$2" --system "$3")
    if [ "$left" != "0" ]; then
      echo "$(date -u +%FT%TZ) $1 $3: stopped with $left left (quota/outage); sleeping 30 min"
      sleep 1800
    fi
  done
  echo "$(date -u +%FT%TZ) $1 $3: complete"
}

mkdir -p var/logs
run_until_done dev-v2  dev  casefile        # finish confirming the dev fixes (frozen from here on)
run_until_done test-v1 test casefile        # the frozen test set; the no-reviewer ablation is derived from it
run_until_done test-v1 test single_prompt
run_until_done test-v1 test single_agent
echo "$(date -u +%FT%TZ) pipeline complete"
