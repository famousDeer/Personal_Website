#!/bin/bash
# Aktualizacja strony na Raspberry Pi.
#
# Buduje i podmienia aplikację tylko wtedy, gdy w repozytorium jest nowy commit,
# czeka, aż strona naprawdę wstanie (healthcheck /healthz), a na końcu usuwa
# stare obrazy, żeby nie zapychały karty SD.
#
#   deploy/update.sh            aktualizacja, jeśli są zmiany
#   deploy/update.sh --force    przebudowa nawet bez nowych commitów
#
# Cron (codziennie o 4:30, log w ~/logs):
#   30 4 * * * /home/dawid/Documents/Personal_Website/deploy/update.sh >> /home/dawid/logs/update.log 2>&1
#
# Skrypt leży w repozytorium, które sam aktualizuje. Cała treść jest w funkcji
# main, bo bash czyta plik w trakcie wykonania: bez tego `git merge` podmieniający
# ten plik w połowie działania mógłby wykonać pomieszane linie.

main() {
    set -euo pipefail

    local app_dir force=0 old new
    app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    [ "${1:-}" = "--force" ] && force=1
    cd "$app_dir"

    # Dwa uruchomienia naraz (cron + ręcznie) budowałyby ten sam obraz dwa razy.
    exec 9>/tmp/personal_website-update.lock
    if ! flock -n 9; then
        log "Inna aktualizacja już trwa, pomijam."
        return 0
    fi

    git fetch --quiet
    old="$(git rev-parse HEAD)"
    new="$(git rev-parse '@{u}')"
    if [ "$old" = "$new" ] && [ "$force" -eq 0 ]; then
        log "Brak zmian (${old:0:7})."
        return 0
    fi

    log "Aktualizacja ${old:0:7} -> ${new:0:7}"
    git merge --ff-only --quiet '@{u}'

    # Tylko Caddy. Obraz PostgreSQL aktualizujemy świadomie: nowa wersja
    # główna (np. 15 -> 16) nie odczyta danych bez migracji.
    docker compose pull --quiet caddy
    docker compose build --quiet web
    # `up` sam czeka na zdrowe web przed startem Caddy (depends_on) i kończy
    # się błędem, gdy aplikacja nie wstanie - obie drogi prowadzą niżej.
    if ! docker compose up -d --remove-orphans db web caddy || ! wait_healthy web 300; then
        log "BŁĄD: aplikacja nie wstała. Ostatnie logi:"
        docker compose logs --tail 60 web || true
        log "Powrót do poprzedniej wersji:"
        log "  git -C $app_dir reset --hard ${old:0:7} && $app_dir/deploy/update.sh --force"
        return 1
    fi
    log "Strona działa (${new:0:7})."

    # Stary obraz aplikacji po przebudowie traci nazwę (<none>) i tylko zajmuje
    # miejsce. Cache budowania z ostatniego tygodnia zostaje: następny build
    # nie instaluje paczek od nowa, jeśli requirements.txt się nie zmienił.
    docker image prune -f > /dev/null
    docker builder prune -f --filter until=168h > /dev/null
    log "Wolne miejsce: $(df -h / | awk 'NR==2 {print $4 " (" $5 " zajęte)"}')"
}

log() {
    echo "[$(date '+%F %T')] $*"
}

# Czeka, aż healthcheck usługi zgłosi "healthy" (najwyżej $2 sekund).
wait_healthy() {
    local service="$1" timeout="$2" cid status waited=0
    cid="$(docker compose ps -q "$service")"
    [ -n "$cid" ] || return 1
    while [ "$waited" -lt "$timeout" ]; do
        status="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo brak)"
        case "$status" in
            healthy) return 0 ;;
            unhealthy) return 1 ;;
        esac
        sleep 5
        waited=$((waited + 5))
    done
    return 1
}

main "$@"
exit $?
