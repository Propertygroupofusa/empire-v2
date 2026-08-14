#!/bin/bash
# Empire v2 Server Watchdog - Keeps the FastAPI server running 24/7
# Auto-restarts on crash, logs all activity, prevents duplicate processes
# chmod +x run_server.sh (execute permission restored)

set -u

SERVER_PID_FILE="/tmp/empire_server.pid"
SERVER_LOG="/tmp/empire_server.log"
ERROR_LOG="/tmp/empire_server_errors.log"
RESTART_DELAY=5
MAX_RESTARTS_PER_HOUR=10
RESTART_WINDOW=3600

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$SERVER_LOG"
}

error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1" | tee -a "$ERROR_LOG"
}

cleanup() {
    log "Shutdown signal received. Stopping server..."
    if [ -f "$SERVER_PID_FILE" ]; then
        PID=$(cat "$SERVER_PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            sleep 2
            kill -9 "$PID" 2>/dev/null || true
        fi
        rm -f "$SERVER_PID_FILE"
    fi
    log "Server stopped."
    exit 0
}

trap cleanup SIGINT SIGTERM

check_process_health() {
    if [ -f "$SERVER_PID_FILE" ]; then
        PID=$(cat "$SERVER_PID_FILE")
        if ! kill -0 "$PID" 2>/dev/null; then
            return 1  # Process dead
        fi
    else
        return 1  # No PID file
    fi

    # Check if HTTP health endpoint responds
    if curl -s http://localhost:8000/health >/dev/null 2>&1; then
        return 0  # Healthy
    else
        return 1  # Unhealthy
    fi
}

start_server() {
    log "Starting FastAPI server..."
    cd /app

    # Start server in background, write PID
    python3 main.py >> "$SERVER_LOG" 2>&1 &
    echo $! > "$SERVER_PID_FILE"

    # Wait for startup
    sleep 3

    # Verify startup (allow 180 seconds for bot initialization)
    local retries=0
    while [ $retries -lt 90 ]; do
        if curl -s http://localhost:8000/health >/dev/null 2>&1; then
            log "✅ Server started successfully (PID: $(cat $SERVER_PID_FILE))"
            return 0
        fi
        sleep 2
        retries=$((retries + 1))
    done

    error "Server failed to start after 180 seconds"
    return 1
}

main() {
    log "Empire v2 Server Watchdog starting..."
    log "Logs: $SERVER_LOG"
    log "Errors: $ERROR_LOG"

    declare -A restart_times

    while true; do
        if ! check_process_health; then
            # Server is down or unhealthy
            error "Server health check failed. Restarting..."

            # Rate limit restarts (max $MAX_RESTARTS_PER_HOUR per hour)
            current_hour=$(date +%s)
            restart_times[$current_hour]=$((${restart_times[$current_hour]:-0} + 1))

            # Clean old restart counts
            for hour in "${!restart_times[@]}"; do
                if [ $((current_hour - hour)) -gt $RESTART_WINDOW ]; then
                    unset restart_times[$hour]
                fi
            done

            if [ ${restart_times[$current_hour]} -gt $MAX_RESTARTS_PER_HOUR ]; then
                error "Too many restarts ($MAX_RESTARTS_PER_HOUR+) in the last hour. Stopping watchdog to prevent restart loop."
                exit 1
            fi

            # Kill old process if still exists
            if [ -f "$SERVER_PID_FILE" ]; then
                kill -9 $(cat "$SERVER_PID_FILE") 2>/dev/null || true
            fi

            sleep $RESTART_DELAY
            start_server || error "Failed to start server"
        fi

        # Check every 30 seconds
        sleep 30
    done
}

main
