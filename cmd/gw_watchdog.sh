#!/bin/bash
cd /  # 防止 uninstall 删除 APP_DIR 后本进程 cwd 指向已删目录，导致 psql/systemctl 报 "could not identify current directory"
# 常驻看门狗：每 120s(及启动时立即)校验飞牛统一网关 entry 的 gateway_socket，
# 若被 appcenter 重建/清空则在 2 分钟内自动补回，用户无感。由 cmd/main 以 setsid 分离启动。
SOCK_TARGET="/var/apps/com.dashboard.nasdash/target/app.sock"
PREFIX="/app/com.dashboard.nasdash"
HEAL_LOG="${TRIM_PKGVAR:-/var/apps/com.dashboard.nasdash/var}/gateway_heal.log"
PSQL_BIN="$(command -v psql 2>/dev/null || echo /usr/bin/psql)"
[ -x "$PSQL_BIN" ] || exit 0

while true; do
    ok=$(sudo -u postgres "$PSQL_BIN" -d trim_sac -tAc \
        "SELECT 1 FROM entry WHERE app_name='com.dashboard.nasdash' \
           AND gateway_socket='$SOCK_TARGET' AND gateway_prefix='$PREFIX' LIMIT 1;" 2>/dev/null)
    if [ "$ok" != "1" ]; then
        if sudo -u postgres "$PSQL_BIN" -d trim_sac -c \
            "UPDATE entry SET gateway_socket='$SOCK_TARGET', gateway_prefix='$PREFIX' \
             WHERE app_name='com.dashboard.nasdash';" >> "$HEAL_LOG" 2>&1; then
            echo "$(date '+%F %T') [watchdog] gateway entry fixed, restarting trim_http_cgi" >> "$HEAL_LOG"
            systemctl restart trim_http_cgi >> "$HEAL_LOG" 2>&1 || \
                echo "$(date '+%F %T') [watchdog] WARN: systemctl restart trim_http_cgi failed" >> "$HEAL_LOG"
        else
            echo "$(date '+%F %T') [watchdog] ERROR: UPDATE entry failed (see psql error above)" >> "$HEAL_LOG"
        fi
    fi
    sleep 30
done
