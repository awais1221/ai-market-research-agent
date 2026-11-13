#!/usr/bin/env bash
# Keeps the agent's SQLite database between GitHub Actions runs.
# It lives on a separate "data" branch, so the main branch history stays clean.
#   scripts/ci_db.sh restore   -> data/agent.db from the data branch (if any)
#   scripts/ci_db.sh save      -> commit data/agent.db to the data branch
set -euo pipefail
mode="${1:?usage: ci_db.sh restore|save}"
repo_url="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
mkdir -p data

if [ "$mode" = "restore" ]; then
  if git ls-remote --exit-code --heads "$repo_url" data >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    git clone --quiet --depth 1 --branch data "$repo_url" "$tmp"
    cp "$tmp/agent.db" data/agent.db
    echo "Restored database ($(du -h data/agent.db | cut -f1))"
  else
    echo "No data branch yet: starting with an empty database"
  fi
elif [ "$mode" = "save" ]; then
  [ -f data/agent.db ] || { echo "No database to save"; exit 0; }
  tmp="$(mktemp -d)"
  cp data/agent.db "$tmp/agent.db"
  cd "$tmp"
  git init --quiet
  git checkout --quiet -b data
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  printf "# Agent database\n\nUpdated automatically by GitHub Actions. Do not edit.\n" > README.md
  git add agent.db README.md
  git commit --quiet -m "Update agent database ($(date -u +%Y-%m-%dT%H:%MZ))"
  # one commit only: the database already contains the full history
  git push --quiet --force "$repo_url" data
  echo "Saved database to the data branch"
fi
