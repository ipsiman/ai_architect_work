#!/usr/bin/env bash
# Бэкап SecureKB: снапшот Qdrant + согласованная копия data/graph.db.
# Использование: bash scripts/backup.sh  (результат — в data/backups/)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATE="$(date +%Y-%m-%d)"
BACKUP_DIR="$ROOT/data/backups"
QDRANT="${QDRANT_URL:-http://localhost:6333}"
COLLECTION="securekb_chunks"

mkdir -p "$BACKUP_DIR"

echo "[backup] 1/2: снапшот Qdrant (коллекция $COLLECTION)..."
curl -fsS -X POST "$QDRANT/collections/$COLLECTION/snapshots" >/dev/null

# последний (самый свежий) снапшот коллекции
SNAP_NAME="$(curl -fsS "$QDRANT/collections/$COLLECTION/snapshots" \
    | jq -r 'if (.result | type) == "array" then .result else .result.snapshots end
             | sort_by(.creation_time // .creation_timestamp_sec) | last | .name')"
if [ -z "$SNAP_NAME" ] || [ "$SNAP_NAME" = "null" ]; then
    echo "[backup] FAIL: не удалось получить список снапшотов Qdrant" >&2
    exit 1
fi
curl -fsS "$QDRANT/collections/$COLLECTION/snapshots/$SNAP_NAME" \
    -o "$BACKUP_DIR/qdrant-$DATE.snapshot"

echo "[backup] 2/2: sqlite3 .backup data/graph.db ..."
sqlite3 "$ROOT/data/graph.db" ".backup '$BACKUP_DIR/graph-$DATE.db'"

echo "[backup] готово, созданные файлы:"
ls -la "$BACKUP_DIR/qdrant-$DATE.snapshot" "$BACKUP_DIR/graph-$DATE.db"
