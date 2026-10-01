#!/bin/bash
# Start strony po włączeniu Raspberry Pi.
#
# Kontenery mają `restart: unless-stopped`, więc Docker uruchamia je sam po
# starcie systemu. Ten skrypt tylko sprawdza, że wszystko wstało, i dopisuje
# wynik do logu. Niczego nie pobiera ani nie buduje: start po zaniku prądu ma
# być szybki i działać bez internetu. Aktualizacje robi deploy/update.sh.
#
# Cron:
#   @reboot /home/dawid/Documents/Personal_Website/deploy/reboot.sh >> /home/dawid/logs/reboot.log 2>&1

main() {
    set -uo pipefail
    cd "$(dirname "${BASH_SOURCE[0]}")/.." || return 1

    log "Start systemu, czekam na Dockera..."
    local i
    for i in $(seq 1 60); do
        docker info > /dev/null 2>&1 && break
        sleep 2
    done

    docker compose up -d db web caddy || { log "BŁĄD: docker compose up"; return 1; }

    local cid status waited=0
    cid="$(docker compose ps -q web)"
    while [ "$waited" -lt 300 ]; do
        status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo brak)"
        if [ "$status" = healthy ]; then
            log "Strona działa."
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    log "BŁĄD: strona nie wstała w 5 minut (stan: $status)."
    docker compose logs --tail 60 web
    return 1
}

log() {
    echo "[$(date '+%F %T')] $*"
}

main "$@"
exit $?
