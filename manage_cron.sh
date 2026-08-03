#!/bin/bash
# Manage the TITO Caribbean & Comoros hourly cron job.
# Usage: ./manage_cron.sh [install|remove|status]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/pipeline.sh"
LOG_DIR="$SCRIPT_DIR/outputs/logs"

# Run at hh:07 — 7-minute delay gives IMERG (4h latency fill) and SCaMPR time to land.
# pipeline.sh also waits internally (WAIT_MINUTES) but cron offset is the primary gate.
CRON_SCHEDULE="7 * * * *"
CRON_JOB="$CRON_SCHEDULE $SCRIPT_PATH"

install_cron() {
    if [ ! -x "$SCRIPT_PATH" ]; then
        echo "✗ pipeline.sh is not executable. Run: chmod +x $SCRIPT_PATH"
        return 1
    fi

    if crontab -l 2>/dev/null | grep -qF "$SCRIPT_PATH"; then
        echo "✓ Cron job already installed:"
        crontab -l | grep "$SCRIPT_PATH"
        return 0
    fi

    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    if [ $? -eq 0 ]; then
        echo "✓ Cron job installed successfully"
        echo "  Schedule : $CRON_SCHEDULE  (every hour at hh:07 UTC)"
        echo "  Script   : $SCRIPT_PATH"
    else
        echo "✗ Failed to install cron job"
        return 1
    fi
}

remove_cron() {
    if ! crontab -l 2>/dev/null | grep -qF "$SCRIPT_PATH"; then
        echo "✓ Cron job not found (already removed or never installed)"
        return 0
    fi

    crontab -l 2>/dev/null | grep -vF "$SCRIPT_PATH" | crontab -
    if [ $? -eq 0 ]; then
        echo "✓ Cron job removed successfully"
    else
        echo "✗ Failed to remove cron job"
        return 1
    fi
}

show_status() {
    echo "=== TITO Cron Job Status ==="
    echo ""

    # Cron service check (cron on Ubuntu/Debian, crond on RHEL/CentOS/Argon)
    if systemctl is-active --quiet cron 2>/dev/null || systemctl is-active --quiet crond 2>/dev/null; then
        echo "✓ Cron service is running"
    else
        echo "⚠ Cron service status unknown (may need: sudo systemctl start crond)"
    fi
    echo ""

    if crontab -l 2>/dev/null | grep -qF "$SCRIPT_PATH"; then
        echo "✓ Cron job is INSTALLED"
        echo ""
        echo "Active entry:"
        crontab -l | grep "$SCRIPT_PATH"
        echo ""
        echo "Recent log files (newest first):"
        ls -lht "$LOG_DIR"/tito_hourly_*.log 2>/dev/null | head -5 \
            || echo "  No logs found yet in $LOG_DIR"
        echo ""
        # Show last 10 lines of the most recent log if it exists
        LAST_LOG=$(ls -t "$LOG_DIR"/tito_hourly_*.log 2>/dev/null | head -1)
        if [ -n "$LAST_LOG" ]; then
            echo "Last 10 lines of $LAST_LOG:"
            tail -10 "$LAST_LOG"
        fi
    else
        echo "✗ Cron job is NOT installed"
        echo "  Run: $0 install"
    fi
}

case "${1:-}" in
    install) install_cron ;;
    remove)  remove_cron  ;;
    status)  show_status  ;;
    *)
        echo "TITO Caribbean & Comoros — Cron Job Manager"
        echo ""
        echo "Usage: $0 [install|remove|status]"
        echo ""
        echo "  install   Install cron job (runs pipeline.sh every hour at hh:07)"
        echo "  remove    Remove the TITO cron job"
        echo "  status    Show installation status and recent log tail"
        echo ""
        echo "Example:"
        echo "  chmod +x $SCRIPT_PATH $0"
        echo "  $0 install"
        exit 1
        ;;
esac
