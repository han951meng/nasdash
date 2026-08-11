#!/bin/bash
cd /  # 防止 uninstall 删除 APP_DIR 后本进程 cwd 指向已删目录，导致 psql/systemctl 报 "could not identify current directory"
# 常驻看门狗：每 120s(及启动时立即)校验飞牛统一网关 entry 的 gateway_socket，
# 若被 appcenter 重建/清空则在 2 分钟内自动补回，用户无感。由 cmd/main 以 setsid 分离启动。
SOCK_TARGET="/var/apps/com.dashboard.nasdash/target/app.sock"
PREFIX="/app/com.dashboard.nasdash"
HEAL_LOG="${TRIM_PKGVAR:-/var/apps/com.dashboard.nasdash/var}/gateway_heal.log"
PSQL_BIN="$(command -v psql 2>/dev/null || echo /usr/bin/psql)"
[ -x "$PSQL_BIN" ] || exit 0

# 限制日志大小，避免长期运行撑爆磁盘。超过 200KB 时只保留最近约 50KB 尾部。
rotate_log() {
    local max=204800
    if [ -f "$HEAL_LOG" ]; then
        local sz
        sz=$(stat -c%s "$HEAL_LOG" 2>/dev/null || echo 0)
        if [ "$sz" -gt "$max" ]; then
            tail -c 51200 "$HEAL_LOG" > "${HEAL_LOG}.tmp" && mv "${HEAL_LOG}.tmp" "$HEAL_LOG"
            echo "$(date '+%F %T') [watchdog] log rotated" >> "$HEAL_LOG"
        fi
    fi
}

while true; do
    rotate_log
    ok=$(sudo -u postgres "$PSQL_BIN" -d trim_sac -tAc \
        "SELECT 1 FROM entry WHERE app_name='com.dashboard.nasdash' \
           AND gateway_socket='$SOCK_TARGET' AND gateway_prefix='$PREFIX' LIMIT 1;" 2>/dev/null)
    if [ "$ok" = "1" ]; then
        # entry 已正确：零打扰、不写日志、不重启网关
        sleep 120
        continue
    fi

    # 需要修复。先精确更新真正需要修复的行。psql -tAc 对 UPDATE 返回命令标签
    # “UPDATE N”（不是纯数字），必须用正则取出 N 再判断，否则 [ N -gt 0 ] 会把
    # “UPDATE 1” 当作非数字误判为 0 行，进而跳过重启网关（导致 404 一直存在）。
    updated=$(sudo -u postgres "$PSQL_BIN" -d trim_sac -tAc \
        "UPDATE entry SET gateway_socket='$SOCK_TARGET', gateway_prefix='$PREFIX' \
         WHERE app_name='com.dashboard.nasdash' \
           AND (gateway_socket IS DISTINCT FROM '$SOCK_TARGET' OR gateway_prefix IS DISTINCT FROM '$PREFIX');" 2>/dev/null)
    updated_num=$(printf '%s' "$updated" | grep -oE '[0-9]+' | head -1)
    if [ -n "$updated_num" ] && [ "$updated_num" -gt 0 ]; then
        echo "$(date '+%F %T') [watchdog] gateway entry fixed ($updated_num row(s)), restarting trim_http_cgi" >> "$HEAL_LOG"
        systemctl restart trim_http_cgi >> "$HEAL_LOG" 2>&1 || \
            echo "$(date '+%F %T') [watchdog] WARN: systemctl restart trim_http_cgi failed" >> "$HEAL_LOG"
    else
        # SELECT 没命中，且 UPDATE 也没改到行：可能是应用已卸载/entry 行不存在，避免反复重启网关。
        echo "$(date '+%F %T') [watchdog] entry not correct but no row updated (updated=${updated:-n/a}), skip restart" >> "$HEAL_LOG"
    fi
    sleep 120
done
