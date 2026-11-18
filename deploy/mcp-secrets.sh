#!/usr/bin/env bash
# Create the bearer credential for remote MCP clients. Run as root after build.
# The credential is kept in a root-only environment file. It is printed to
# stdout once so it can be copied into the bot's MCP connector. Status text
# goes to stderr, and the key is never passed as an argument.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
bundle=build/deploy

if [ "$(id -u)" -ne 0 ]; then
  echo "run with sudo: sudo ./deploy/mcp-secrets.sh" >&2
  exit 2
fi
if [ ! -f "$bundle/install.env" ]; then
  echo "no bundle: run ./deploy/build.sh first, as your normal user" >&2
  exit 2
fi
# shellcheck source=/dev/null
. "$bundle/install.env"

rotate=no
case "${1:-}" in
  "") ;;
  --rotate) rotate=yes ;;
  *) echo "usage: sudo ./deploy/mcp-secrets.sh [--rotate]" >&2; exit 2 ;;
esac

install -d -o root -g root -m 0700 "$TRAINER_SECRETS_DIR"
dest="$TRAINER_SECRETS_DIR/mcp.env"
if [ -f "$dest" ] && [ "$rotate" = no ]; then
  echo "MCP credential already exists; pass --rotate to replace it" >&2
  exit 2
fi

key=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
tmp=$(mktemp "$TRAINER_SECRETS_DIR/.mcp.XXXXXX")
cleanup() {
  rm -f "$tmp"
}
trap cleanup EXIT
printf 'TRAINER_MCP_KEY=%s\n' "$key" > "$tmp"
chmod 0600 "$tmp"
chown root:root "$tmp"
mv "$tmp" "$dest"
trap - EXIT

# Show the key before restarting. A failed restart must not be the only reason
# the operator never sees it; the file above still holds it, mode 0600.
printf 'MCP bearer key (shown once): %s\nEndpoint: https://<machine>.<tailnet>.ts.net/mcp\n' "$key"

if systemctl is-enabled --quiet trainer-api.service 2>/dev/null; then
  systemctl restart trainer-api.service
  echo "restarted trainer-api.service" >&2
fi
