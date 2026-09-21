#!/bin/sh
# The published image ships a PLACEHOLDER item catalogue (15 hand-written rows): the full one is
# generated from a CC BY-NC-SA source, which we do not redistribute. See data/ATTRIBUTION.md.
#
# The image build already tries to generate it. If that failed (no network at build time), try
# once more here, on first start, and write the result into the data volume so it survives.
# A failure is never fatal: without a catalogue the connector resolves inventory rows by their
# template id instead of by name, and every other capability is unaffected.
set -eu

ITEMS="${DUNE_ITEMS_FILE:-/app/data/items.json}"
DIR="$(dirname "$ITEMS")"

if [ "${DUNE_CATALOGUE_AUTOGEN:-1}" = "1" ] && grep -q '"placeholder": true' "$ITEMS" 2>/dev/null; then
  echo "catalogue: $ITEMS is the placeholder, generating the full one (see data/ATTRIBUTION.md)"
  if node /app/scripts/gen-catalogue.mjs --out "$DIR" 2>&1; then
    echo "catalogue: generated into $DIR"
  else
    echo "catalogue: generation failed, keeping the placeholder." >&2
    echo "catalogue: items resolve by template id, not by display name. Re-run later with" >&2
    echo "catalogue:   docker compose exec <this-service> node /app/scripts/gen-catalogue.mjs --out $DIR" >&2
  fi
fi

exec "$@"
