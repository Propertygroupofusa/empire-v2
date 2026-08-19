#!/usr/bin/env bash
# Simple egress connectivity checks (run from inside the Railway service/shell)
# Usage: bash scripts/check_egress.sh

set -euo pipefail

hosts=(
  "https://api.alpaca.markets"
  "https://data.alpaca.markets"
  "https://api.coinbase.com"
  "https://api.polygon.io"
  "https://stripe.com"
  "https://github.com"
  "https://raw.githubusercontent.com"
)

smtp_host="smtp.gmail.com"
smtp_port=587

echo "Starting egress checks..."
echo

for url in "${hosts[@]}"; do
  printf "Checking %s ... " "$url"
  if curl -sS --head --max-time 10 "$url" >/dev/null 2>&1; then
    echo "OK"
  else
    echo "FAILED"
  fi
done

printf "Checking SMTP (%s:%s) ... " "$smtp_host" "$smtp_port"
if command -v nc >/dev/null 2>&1; then
  if nc -z -w5 "$smtp_host" "$smtp_port" >/dev/null 2>&1; then
    echo "OK (port open)"
  else
    echo "FAILED (port closed)"
  fi
else
  # Fallback: try openssl if available
  if command -v openssl >/dev/null 2>&1; then
    if timeout 5 bash -c "echo >/dev/tcp/$smtp_host/$smtp_port" 2>/dev/null; then
      echo "OK (port open)"
    else
      echo "FAILED (port closed or /dev/tcp not available)"
    fi
  else
    echo "SKIPPED (nc and openssl not installed — install nc or run an openssl test)"
  fi
fi

echo
echo "Done. If any checks FAILED, verify the Railway Egress Allowlist includes the exact host (or wildcard) and required outbound ports (TCP 443 for HTTPS; TCP 587/465 for SMTP)."
