#!/bin/bash
# Fast deploy for ZK FAN Supplier & Stock (PythonAnywhere).
# Pulls the code files listed in deploy-files.txt from the public,
# code-only GitHub repo and reloads the web app. NO business data
# lives in that repo — data stays in data/supplier_stock.db here.
set -e
cd "$(dirname "$0")"
BASE="https://raw.githubusercontent.com/xohaibkhalid-tech/zkfan-supplier-stock-app/main"
CB="?v=$(date +%s)"
wget -q -O deploy-files.txt "$BASE/deploy-files.txt$CB"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  mkdir -p "$(dirname "$f")"
  if wget -q -O "$f.tmpdl" "$BASE/$f$CB"; then
    mv "$f.tmpdl" "$f"
  else
    echo "FAILED: $f"
    exit 1
  fi
done < deploy-files.txt
python3 -m py_compile app.py db.py
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
touch /var/www/zohaibkhalid_pythonanywhere_com_wsgi.py
echo "DEPLOY_OK"
