#!/usr/bin/env bash
# Bake published CMS content into the static pages, then commit and push so
# the deployed site (Vercel) picks up the changes automatically.
set -euo pipefail
cd "$(dirname "$0")"

python3 cms/annotate.py
python3 cms/build.py

git add -A
if git diff --cached --quiet; then
  echo "No changes to publish."
  exit 0
fi

read -r -p "Commit message (default: \"Publish CMS content updates\"): " MSG
MSG="${MSG:-Publish CMS content updates}"

git commit -m "$MSG"
git push origin main
echo "Pushed to origin/main."