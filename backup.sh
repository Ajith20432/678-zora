#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
out="backups/zora-$(date -u +%Y%m%dT%H%M%SZ).tgz"
mkdir -p backups
docker volume inspect zora_data >/dev/null 2>&1 || { echo "volume zora_data not found: nothing to back up (is ZORA deployed?)"; exit 1; }
docker run --rm -v zora_data:/data -v "$PWD/backups":/backup alpine sh -c "tar czf /backup/$(basename "$out") -C /data ."
echo "Backup: $out"
