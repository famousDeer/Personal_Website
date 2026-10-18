#!/usr/bin/env bash
# Przenosi zdjęcia zatwierdzonych paragonów z Pi na Dysk Google.
# `rclone move` kasuje plik lokalnie dopiero po udanym wysłaniu, więc
# na karcie SD zdjęcia nie zostają, a przy braku internetu nic nie ginie.
#
# Cron na Pi (np. co godzinę):
#   15 * * * * /home/pi/Website-Finance/deploy/receipts_upload.sh >> /home/pi/receipts_upload.log 2>&1
#
# RCLONE_REMOTE: ten sam remote, którego używa backup bazy (rclone listremotes).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTBOX="${RECEIPT_OUTBOX:-$APP_DIR/private_media/receipts_outbox}"
REMOTE="${RCLONE_REMOTE:-gdrive:Website-Finance/paragony}"

[ -d "$OUTBOX" ] || exit 0
# Zdjęcia układają się w folderach rok/miesiąc z nazwy pliku (RRRR-MM-DD_...).
for file in "$OUTBOX"/*.jpg; do
    [ -e "$file" ] || continue
    name="$(basename "$file")"
    year="${name:0:4}"; month="${name:5:2}"
    rclone move "$file" "$REMOTE/$year/$month/" --no-traverse
done
