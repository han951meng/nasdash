#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 硬件监控面板 (fnOS Hardware Dashboard)
单文件 Flask 应用：阵列卡状态 / 硬盘 SMART / 系统资源 / 存储卷
部署目录: /opt/fnos-dash/
"""
import subprocess, json, re, os, time, socket, platform, shutil, sys, glob, functools, urllib.request, urllib.error
from flask import Flask, jsonify, render_template, render_template_string, request, make_response, Response, stream_with_context
from functools import wraps

app = Flask(__name__)

# ===================== 飞牛统一网关用户身份 =====================
# 官方文档要求：访问经网关时，fnOS 先校验登录态，再通过 Header 转发用户信息
# （X-Trim-Userid / X-Trim-Isadmin / X-Trim-Username）。应用必须以网关转发
# Header 为准，「不要信任客户端传入的用户 ID」。本应用仅经统一网关暴露
# （裸端口 9800 仅本地兜底、不对外），故浏览器请求必带这些 Header。
def get_gateway_user():
    """读取网关转发的可信用户上下文；未经过网关时 uid 为空（authenticated=False）。"""
    h = request.headers
    uid = (h.get("X-Trim-Userid") or "").strip()
    return {
        "uid": uid or None,
        "username": (h.get("X-Trim-Username") or "").strip() or None,
        "isAdmin": (h.get("X-Trim-Isadmin") or "").strip().lower() == "true",
        "authenticated": bool(uid),  # 有 Userid 即视为网关已校验登录态
    }

def require_admin():
    """装饰器：要求经网关鉴权且为管理员，否则 403。
    用于所有配置写入 / 硬件控制类「管理接口」（文档：管理接口需要管理员身份）。"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = get_gateway_user()
            if not u["authenticated"]:
                return jsonify({"ok": False, "error": "unauthorized: gateway login required"}), 403
            if not u["isAdmin"]:
                return jsonify({"ok": False, "error": "forbidden: admin required"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.route("/api/me")
def api_me():
    """返回当前网关登录用户，供前端展示登录身份。"""
    return jsonify(get_gateway_user())

# 应用根目录
APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 前端模板目录（index() 用 render_template 渲染 templates/index.html）
app.template_folder = os.path.join(APP_DIR, "templates")

# 用户配置持久目录：飞牛运行时通过环境变量 TRIM_PKGVAR 提供 @appdata 持久目录
# （与应用卸载无关，重装后保留；cmd/main 也用它存 app.pid/app.log）。
# 早期版本把配置写在 APP_DIR，导致每次重装被清空。现统一写入此持久目录，重装不丢配置。
# LEGACY_APPATA 是旧固件/旧安装下 _config_dir 的硬编码兜底位置（当 TRIM_PKGVAR 未注入时），
# 与运行时 TRIM_PKGVAR（如 /vol1/@appdata/...）可能不在同一物理路径；
# 重装迁移时须把它也当作旧配置来源，否则标注会孤儿在旧目录导致 UI 空白。
LEGACY_APPATA = "/usr/local/apps/@appdata/com.dashboard.nasdash"
def _config_dir():
    d = os.environ.get("TRIM_PKGVAR")
    if not d:
        d = LEGACY_APPATA
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return APP_DIR

# 从旧版（配置存 APP_DIR 或旧 @appdata 兜底目录）升级时，把已有配置迁移到当前持久目录，避免丢失
def _migrate_legacy_configs():
    cfg = _config_dir()
    if cfg == APP_DIR:
        return
    # 旧配置可能来源（按陈旧程度排序；跳过与当前目标相同的目录，避免无意义的自复制）
    legacy_sources = [APP_DIR, LEGACY_APPATA]
    for name in ("board_override.txt", "fan_labels.json", "fan_disk_temp.json", "fan_sys_temp.json"):
        dst = os.path.join(cfg, name)
        if os.path.exists(dst):
            continue
        for src_dir in legacy_sources:
            if src_dir == cfg:
                continue
            src = os.path.join(src_dir, name)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
                break

_migrate_legacy_configs()

BOARD_OVERRIDE_FILE = os.path.join(_config_dir(), "board_override.txt")

# 版本号单一来源：fnOS 标准安装时 manifest 不在 APP_DIR（APP_DIR 只有 app.tgz 内容），
# 而是在 /var/apps/<appid>/manifest。两个位置都查，最后才回退硬编码值（曾因只查 APP_DIR 导致所有标准安装都显示 v1.6.2）。
def _app_version():
    appid = os.path.basename(APP_DIR)  # 如 com.dashboard.nasdash
    candidates = [
        os.path.join("/var/apps", appid, "manifest"),
        os.path.join(APP_DIR, "manifest"),
    ]
    for path in candidates:
        try:
            with open(path) as f:
                m = re.search(r"^version\s*=\s*(\S+)", f.read(), re.M)
                if m:
                    return "v" + m.group(1).strip()
        except Exception:
            pass
    return "v1.6.2"
APP_VERSION = _app_version()

# ---------- 检测新版本（GitHub latest release，带缓存/超时/静默失败，绝不阻塞页面） ----------
_VERSION_CHECK = {"cached_result": None, "checked_at": 0}
_VERSION_CHECK_TTL = 6 * 3600  # 6 小时缓存，避免频繁打 GitHub API
_VERSION_REPO_URL = "https://api.github.com/repos/han951meng/nasdash/releases/latest"

def _parse_ver(v):
    """'v1.6.7' / '1.6.7' -> (1,6,7)"""
    v = (v or "").lstrip("vV").strip()
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:3]) if parts else (0, 0, 0)

def _check_latest_version():
    """查询 GitHub latest release；带缓存、5s 超时、异常静默返回。"""
    now = time.time()
    if _VERSION_CHECK["cached_result"] is not None and now - _VERSION_CHECK["checked_at"] < _VERSION_CHECK_TTL:
        return _VERSION_CHECK["cached_result"]
    result = {"current": APP_VERSION, "latest": None, "update_available": False, "url": None, "error": None}
    try:
        req = urllib.request.Request(_VERSION_REPO_URL, headers={"User-Agent": "nasdash-version-check"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        latest = ("v" + tag.lstrip("vV")) if tag else ""
        result["latest"] = latest
        result["url"] = data.get("html_url") or "https://github.com/han951meng/nasdash/releases"
        result["update_available"] = _parse_ver(latest) > _parse_ver(APP_VERSION)
    except Exception as e:
        result["error"] = str(e)[:160]
    _VERSION_CHECK["cached_result"] = result
    _VERSION_CHECK["checked_at"] = now
    return result

# 命令全路径（admin 的 PATH 不含 /usr/sbin）
def _find_storcli():
    """动态探测 storcli 二进制：兼容只装了 storcli64 的环境（部分 fnOS 用户机器只有 storcli64）"""
    candidates = [
        "/usr/local/bin/storcli64",
        "/usr/local/bin/storcli",
        "/opt/MegaRAID/storcli/storcli64",
        "/opt/MegaRAID/storcli/storcli",
        "/usr/sbin/storcli64",
        "/usr/sbin/storcli",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return shutil.which("storcli64") or shutil.which("storcli") or ""

STORCLI = _find_storcli()
SMARTCTL = "/usr/sbin/smartctl"
SENSORS = "/usr/bin/sensors"
DMIDECODE = "/usr/sbin/dmidecode"

# ---------- 基础执行 ----------
def log(msg):
    """轻量日志：追加到应用目录 debug.log，便于排查静默失败（如 storcli 命令执行失败）"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(APP_DIR, "debug.log"), "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ===================== 安全执行封装（shell=False，杜绝命令注入）=====================
# 原 run()/sudo() 用 shell=True + 字符串拼命令，一旦拼接系统枚举值即存在注入面。
# 现统一改用 run_cmd/sudo_cmd（参数列表 + shell=False）；读文件直接 open() 不用 shell。
def read_file(path, default=""):
    """安全读取文件内容（替代 run('cat ...')）。路径须为受控系统路径。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return default

def _safe_token(s, maxlen=128):
    """校验系统枚举值（设备名/hwmon 路径/容器名等）只含安全字符，防路径/命令注入。
    返回原值或 None（非法）。"""
    if not isinstance(s, str) or not s or len(s) > maxlen:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_./@:-]+", s):
        return None
    return s

def _run_raw(args, timeout=30, as_root=False):
    try:
        args = [str(a) for a in args]
        # 裸命令名按 PATH 解析（还原 shell=True 旧行为：fnOS 默认 PATH 不含 /usr/sbin，
        # 旧代码靠 shell 找 ip/lspci 等；此处仅解析路径，不拼接字符串，无注入面）
        if args and "/" not in args[0]:
            _resolved = shutil.which(args[0])
            if _resolved:
                args[0] = _resolved
        if as_root and os.geteuid() != 0:
            args = ["sudo", "-n"] + args
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and r.stderr.strip():
            log("cmd failed (rc=%d): %s\n%s" % (r.returncode, " ".join(args), r.stderr.strip()))
        return r.stdout
    except Exception as e:
        log("cmd error: %s\n%s" % (" ".join(map(str, args)), e))
        return ""

def run_cmd(args, timeout=30):
    """shell=False 执行（推荐）：args 为参数列表，不接受字符串。"""
    return _run_raw(args, timeout, as_root=False)

def sudo_cmd(args, timeout=30):
    """shell=False 执行需 root 的命令（自动 passwordless sudo 兜底）。"""
    return _run_raw(args, timeout, as_root=True)

# ===================== JSON 配置读写（统一）=====================
def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default

def _save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

# ===================== 温控曲线配置套用（两套对称，去重）=====================
def _apply_temp_curve(cfg, data, recover_max=100):
    """把 HTTP 配置体套用到 cfg（disk_temp / sys_temp 共用）。返回错误响应或 None。"""
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    for k in ("start_temp", "full_temp", "min_pwm", "max_pwm"):
        if k in data:
            try:
                v = float(data[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": k + " 需为数字"}), 400
            if k in ("min_pwm", "max_pwm") and (v < 0 or v > 100):
                return jsonify({"ok": False, "error": k + " 需在 0~100"}), 400
            cfg[k] = v
    if "recover_temp" in data:
        try:
            rv = float(data["recover_temp"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "recover_temp 需为数字"}), 400
        if rv < 0 or rv > recover_max:
            return jsonify({"ok": False, "error": "recover_temp 需在 0~%d" % recover_max}), 400
        cfg["recover_temp"] = rv
    if "controlled_fans" in data:
        cf = data["controlled_fans"]
        if cf == "all":
            cfg["controlled_fans"] = "all"
        elif isinstance(cf, list):
            norm = []
            for pair in cf:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    try:
                        norm.append([str(pair[0]), int(pair[1])])
                    except (TypeError, ValueError):
                        return jsonify({"ok": False, "error": "controlled_fans 每项需为 [hwmon, idx]"}), 400
                else:
                    return jsonify({"ok": False, "error": "controlled_fans 每项需为 [hwmon, idx]"}), 400
            cfg["controlled_fans"] = norm
        else:
            return jsonify({"ok": False, "error": "controlled_fans 需为 'all' 或 [[hwmon,idx],...]"}), 400
    if "curve" in data:
        # 自定义温度→PWM 曲线：[[温度, 占空比], ...]，至少 2 点；缺失时回退 start/full 线性
        curve = data["curve"]
        if not isinstance(curve, list):
            return jsonify({"ok": False, "error": "curve 需为数组"}), 400
        norm = []
        for p in curve:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                return jsonify({"ok": False, "error": "curve 每项需为 [温度, 占空比]"}), 400
            try:
                t = float(p[0]); pw = float(p[1])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "curve 温度/占空比需为数字"}), 400
            if pw < 0 or pw > 100:
                return jsonify({"ok": False, "error": "curve 占空比需在 0~100"}), 400
            norm.append([t, pw])
        norm.sort(key=lambda x: x[0])
        cfg["curve"] = norm
    st, ft, rt = cfg.get("start_temp"), cfg.get("full_temp"), cfg.get("recover_temp")
    if ft is not None and st is not None and ft <= st:
        return jsonify({"ok": False, "error": "full_temp 必须大于 start_temp"}), 400
    if rt is not None and st is not None and rt >= st:
        return jsonify({"ok": False, "error": "recover_temp 必须小于 start_temp"}), 400
    return None

# ===================== TTL 缓存装饰器（复用 _FAN_ENUM_CACHE 思路）=====================
def _ttl_cache(ttl):
    def deco(fn):
        store = {}
        @functools.wraps(fn)
        def wrapper(*a, **k):
            key = (a, tuple(sorted(k.items())))
            now = time.time()
            if key in store and now - store[key][0] < ttl:
                return store[key][1]
            val = fn(*a, **k)
            store[key] = (now, val)
            return val
        return wrapper
    return deco

# ===================== 风扇缓变控制（常驻线程平滑过渡，避免瞬间全速）=====================
import threading as _threading
import glob as _glob

# 全局风扇目标状态：key=(hwmon, idx) -> {"mode":"manual"|"auto", "target":0-255}
FAN_LOCK = _threading.Lock()
FAN_TARGETS = {}
_FAN_LAST_CPU_TEMP = {"t": 0.0, "v": None}
# 本机真实风扇全集缓存（拓扑基本静态，30s 刷新；见 _enumerate_fans）
_FAN_ENUM_CACHE = {"t": 0.0, "v": []}

def _fan_read_raw(hwmon, idx):
    try:
        with open(f"{hwmon}/pwm{idx}") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _fan_write_raw(hwmon, idx, raw):
    raw = max(0, min(255, int(raw)))
    try:
        with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
            f.write("1")
        with open(f"{hwmon}/pwm{idx}", "w") as f:
            f.write(str(raw))
        return True
    except Exception:
        return False

def _fan_ext_service_running():
    # 检测系统风扇服务（pwm-fancontrol，fnOS 自带）是否处于 active 状态。
    # fnOS 的风扇服务是 oneshot 服务（跑完写一次 PWM 即退出，非常驻进程），
    # 用 systemctl is-active 判断其 active(exited) 状态比 pgrep 进程更可靠。
    out = run_cmd(["systemctl", "is-active", "pwm-fancontrol"], 2).strip().lower()
    return out in ("active", "activating")

# 接管 / 交还系统风扇服务（FanControlServer）：
# 本应用与 fnOS 自带的 FanControlServer 都直接写 /sys/class/hwmon/.../pwmN，
# 二者同时运行会抢控 → 风扇转速抖动甚至被对方覆盖。论坛亦有用户反馈此冲突。
# 故采用「接管即停、全交还即恢复」策略：nasdash 真正在控速任意风扇时停掉 FCS，
# 全部交还自动后重启 FCS，恢复 fnOS 原生控温。全程 best-effort，失败静默，
# 绝不因停/启服务异常而中断风扇调速主流程。默认（用户未启用任何控速）不触碰 FCS。
_FCS_TAKEN = {"v": False}
# 用户可在面板「永久禁用」FanControlServer（stop+disable，重启不复活）；持久化到 @appdata。
# 为 True 时，nasdash 交还自动控温后不再把 FCS 拉起来，尊重用户选择。
FCS_STATE_FILE = os.path.join(_config_dir(), "fcs_state.json")

def _fcs_disabled():
    """用户是否已在面板永久禁用 FanControlServer（读持久化标志）。"""
    return bool(_load_json_file(FCS_STATE_FILE, {}).get("disabled"))

def _set_fcs_disabled(v):
    return _save_json_file(FCS_STATE_FILE, {"disabled": bool(v)})

def _fcs_installed_state():
    """systemctl is-enabled 的原始结果（enabled/disabled/masked/static/...；未安装为空串）。"""
    return run_cmd(["systemctl", "is-enabled", "pwm-fancontrol"], 3).strip().lower()

def _fcs_status():
    """汇总 FanControlServer 状态供面板展示：是否安装/是否开机自启/是否在跑/是否被用户永久禁用。"""
    raw = _fcs_installed_state()
    installed = raw in ("enabled", "disabled", "masked", "static", "indirect",
                        "enabled-runtime", "linked", "generated", "alias")
    return {
        "installed": installed,
        "enabled": raw == "enabled",
        "running": _fan_ext_service_running(),
        "disabled_by_user": _fcs_disabled(),
        "raw": raw,
    }

def _fan_stop_ext_service():
    """临时停止系统风扇服务 FanControlServer（接管窗口内，仅 best-effort）。"""
    try:
        sudo_cmd(["systemctl", "stop", "pwm-fancontrol"], 5)
    except Exception:
        pass
    try:
        sudo_cmd(["pkill", "-f", "pwm-fancontrol"], 2)
    except Exception:
        pass

def _fan_start_ext_service():
    """交还自动时重启系统风扇服务 FanControlServer（仅 best-effort）。
    若用户已在面板「永久禁用」FCS，则不再拉起，尊重用户选择。"""
    if _fcs_disabled():
        return
    try:
        sudo_cmd(["systemctl", "start", "pwm-fancontrol"], 5)
    except Exception:
        pass

def _fcs_disable():
    """永久禁用 FanControlServer：stop + disable（重启不复活）+ 持久化标志。
    即便 systemctl 命令异常也会写标志，确保 nasdash 交还逻辑不再拉起 FCS。"""
    ok = False
    try:
        sudo_cmd(["systemctl", "disable", "--now", "pwm-fancontrol"], 8)
        ok = True
    except Exception:
        pass
    try:
        sudo_cmd(["pkill", "-f", "pwm-fancontrol"], 2)  # 兜底杀非 systemd 残留进程
    except Exception:
        pass
    _set_fcs_disabled(True)
    _FCS_TAKEN["v"] = False
    return ok

def _fcs_enable():
    """恢复 FanControlServer：清除持久化标志 + enable + start。"""
    _set_fcs_disabled(False)
    ok = False
    try:
        sudo_cmd(["systemctl", "enable", "--now", "pwm-fancontrol"], 8)
        ok = True
    except Exception:
        pass
    return ok

# ===================== 风扇模式持久化（启动自动恢复，避免重启后全速）=====================
# 按 idx（非 hwmon 路径）持久化，抗 hwmon 跨重启漂移。结构：{str(idx): {"mode":"auto"|"manual", "target":0-255}}
FAN_MODE_FILE = os.path.join(_config_dir(), "fan_mode.json")

def _load_fan_modes():
    return _load_json_file(FAN_MODE_FILE, {})

def _save_fan_modes(modes):
    return _save_json_file(FAN_MODE_FILE, modes)

def _save_fan_mode(idx, mode, target):
    """用户经 UI 设过某风扇模式后调用，持久化以便重启自动恢复。"""
    try:
        modes = _load_fan_modes()
        modes[str(int(idx))] = {"mode": mode, "target": (int(target) if target is not None else None)}
        _save_fan_modes(modes)
    except Exception:
        pass

def _fan_read_cpu_temp():
    try:
        out = run_cmd(["sensors", "-j"], 5)
        j = json.loads(out)
        for chip, entries in j.items():
            if chip.startswith("coretemp"):
                for ename, fields in entries.items():
                    if isinstance(fields, dict) and "Package" in ename:
                        for k, v in fields.items():
                            if k.startswith("temp") and k.endswith("_input"):
                                return float(v)
    except Exception:
        pass
    return None

def _fan_cpu_temp_cached():
    now = time.time()
    if now - _FAN_LAST_CPU_TEMP["t"] > 2:
        _FAN_LAST_CPU_TEMP["v"] = _fan_read_cpu_temp()
        _FAN_LAST_CPU_TEMP["t"] = now
    return _FAN_LAST_CPU_TEMP["v"]

def _fan_auto_pwm(cpu_temp):
    # nasdash 自带保守温控曲线（系统风扇服务不在时接管）
    pts = [(45, 90), (60, 140), (75, 204), (80, 255)]
    if cpu_temp is None:
        raw = pts[0][1]
    elif cpu_temp <= pts[0][0]:
        raw = pts[0][1]
    elif cpu_temp >= pts[-1][0]:
        raw = pts[-1][1]
    else:
        raw = pts[0][1]
        for i in range(len(pts) - 1):
            t0, p0 = pts[i]
            t1, p1 = pts[i + 1]
            if cpu_temp <= t1:
                r = (cpu_temp - t0) / (t1 - t0)
                raw = int(p0 + r * (p1 - p0))
                break
    # clamp 30%~70%（raw 76~178），防止 auto 狂转（旧机 IT87 曾因此满速）
    return max(76, min(178, raw))

def _fan_smooth_step(hwmon, idx, target):
    _fan_set_enable(hwmon, idx, 1)   # 接管写 pwm 前确保软件控（enable=1）；否则硬件 enable=2 时写 pwm 被内核忽略→全速
    cur = _fan_read_raw(hwmon, idx)
    if cur is None:
        return
    diff = target - cur
    if abs(diff) <= 2:  # deadzone，避免抖动
        if cur != target:
            _fan_write_raw(hwmon, idx, target)
        return
    # 每 tick 最多变 18（≈ 12%/秒），手动拉进度条几秒内明显响应，又不至于瞬间从静音直接满速。
    step = 18 if abs(diff) > 18 else abs(diff)
    _fan_write_raw(hwmon, idx, cur + (step if diff > 0 else -step))

def _enumerate_fans(force=False):
    """枚举本机所有「可控制风扇通道」(hwmon_path, idx)。

    自动检测设计（换硬件不失效）：
    - 不依赖芯片型号白名单（it87 / nct / fintek / winbond / asus / AMD 等皆可），
      只要某个 hwmon 暴露 pwm<N>_enable 且存在对应 pwm<N> / fan<N>_input，
      就视为一个可控制风扇通道；
    - 遍历所有 /sys/class/hwmon/hwmon*（多风扇芯片主板不漏）；
    - 通道号 1..10（覆盖主板直连 + 集线器 / 分线器扩展）。
    作为温控循环的控制全集；FAN_TARGETS 仅作「每风扇手动/自动覆盖映射」。
    修复 Bug A：此前温控循环只遍历 FAN_TARGETS（仅用户手动调过的风扇才填充），
    导致 sys_temp/disk_temp 设 controlled_fans:"all" 时启动即空转、一个风扇都不控。
    拓扑基本静态，缓存 30s 刷新一次（支持热插拔风机后自动纳入）。"""
    now = time.time()
    if not force and now - _FAN_ENUM_CACHE["t"] < 30:
        return _FAN_ENUM_CACHE["v"]
    fans = []
    try:
        for _hp in sorted(_glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                _pes = _glob.glob(f"{_hp}/pwm*_enable")
            except Exception:
                continue
            for _pe_path in _pes:
                _m = re.search(r"pwm(\d+)_enable$", _pe_path)
                if not _m:
                    continue
                _fi = int(_m.group(1))
                if _fi > 10:
                    continue
                # 佐证文件存在：pwm<N> 或 fan<N>_input，排除非风扇 pwm（如 RGB 灯效）
                if not (os.path.exists(f"{_hp}/pwm{_fi}") or os.path.exists(f"{_hp}/fan{_fi}_input")):
                    continue
                fans.append((_hp, _fi))
    except Exception:
        pass
    _FAN_ENUM_CACHE["t"] = now
    _FAN_ENUM_CACHE["v"] = fans
    return fans

def _select_temp_fans(all_fans, sys_cfg, disk_cfg):
    """按 controlled_fans 配置从 all_fans 中选出被 sys_temp / disk_temp 接管的风扇集合。
    选择依据「真实风扇全集 all_fans」而非 FAN_TARGETS —— 这是 Bug A 修复的核心。"""
    sys_claimed = set()
    disk_claimed = set()
    if sys_cfg.get("enabled"):
        cf = sys_cfg.get("controlled_fans", "all")
        for (hwmon, idx) in all_fans:
            if cf != "all" and [hwmon, idx] not in cf:
                continue
            sys_claimed.add((hwmon, idx))
    if disk_cfg.get("enabled") and disk_cfg.get("disks"):
        cf = disk_cfg.get("controlled_fans", "all")
        for (hwmon, idx) in all_fans:
            if (hwmon, idx) in sys_claimed:
                continue
            if cf != "all" and [hwmon, idx] not in cf:
                continue
            disk_claimed.add((hwmon, idx))
    return sys_claimed, disk_claimed

def _restore_fan_modes():
    """nasdash 启动后自动恢复风扇模式，避免重启后 FAN_TARGETS 空导致风扇保持硬件全速：
    - 有持久化配置：按用户上次选择恢复（auto 接管；若 FCS 在控则交还 enable=2；manual 恢复固定值）。
    - 无配置（首次/清配置）：若系统风扇服务 FCS 未在控，默认将所有可控风扇设为 auto 接管，
      消除开机全速；若 FCS 在控则交还、不强行接管（尊重 fnOS 原生控温）。"""
    try:
        enum = _enumerate_fans()
        idx2hw = {i: h for (h, i) in enum}
        if not idx2hw:
            return
        fcs = _fan_ext_service_running()
        modes = _load_fan_modes()
        if not modes:
            # 首次/无配置：FCS 未控则默认接管 auto；FCS 在控则交还、不抢
            if not fcs:
                for idx, hwmon in idx2hw.items():
                    with FAN_LOCK:
                        FAN_TARGETS[(hwmon, idx)] = {"mode": "auto", "target": None}
            else:
                for idx, hwmon in idx2hw.items():
                    try:
                        with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
                            f.write("2")
                    except Exception:
                        pass
            return
        # 已有配置：按用户上次选择恢复
        for sidx, m in modes.items():
            try:
                idx = int(sidx)
            except Exception:
                continue
            if idx not in idx2hw:
                continue
            hwmon = idx2hw[idx]
            mode = m.get("mode")
            if mode == "auto":
                if fcs:
                    try:
                        with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
                            f.write("2")
                    except Exception:
                        pass
                else:
                    with FAN_LOCK:
                        FAN_TARGETS[(hwmon, idx)] = {"mode": "auto", "target": None}
            elif mode == "manual":
                tgt = m.get("target")
                if tgt is None:
                    continue
                with FAN_LOCK:
                    FAN_TARGETS[(hwmon, idx)] = {"mode": "manual", "target": int(tgt)}
        # 配置未列出、但本机枚举到的可控风扇（启动期 hwmon 晚注册 / 新装风扇），
        # 默认接管为 auto（FCS 未控时），避免留下「失控 / 开机狂转」的风扇。
        if not fcs:
            for idx, hwmon in idx2hw.items():
                with FAN_LOCK:
                    if (hwmon, idx) not in FAN_TARGETS:
                        FAN_TARGETS[(hwmon, idx)] = {"mode": "auto", "target": None}
    except Exception:
        pass

def _fan_ensure_all_claimed():
    """每轮兜底：把枚举到的、尚未被接管的可控风扇默认设为 auto 接管。
    解决硬重启时序（hwmon 晚于 nasdash 自启注册、启动期枚举不全）与 fan_mode.json 不完整
    导致的「部分风扇失控、开机狂转」。已显式设为 manual 的风扇在 FAN_TARGETS 中会被跳过，不被覆盖。"""
    try:
        if _fan_ext_service_running():
            return
        enum = _enumerate_fans()
        with FAN_LOCK:
            for (hwmon, idx) in enum:
                if (hwmon, idx) not in FAN_TARGETS:
                    FAN_TARGETS[(hwmon, idx)] = {"mode": "auto", "target": None}
    except Exception:
        pass

def fan_smooth_loop():
    # daemon 线程：每 ~0.6s 把风扇当前 pwm 朝目标平滑过渡（常驻线程 tick + 缓变）
    while True:
        try:
            # 自愈：每轮确保本机枚举到的每个可控风扇都被接管为 auto（除非用户显式设为 manual）。
            # 解决硬重启时序（hwmon 晚于 nasdash 自启注册、启动期枚举不全）与 fan_mode.json 不完整
            # 导致的「部分风扇失控、开机狂转」。FCS 在控时不抢（交还原生控温）。
            _fan_ensure_all_claimed()
            with FAN_LOCK:
                overrides = dict(FAN_TARGETS)   # 每风扇手动/自动覆盖（仅用户经 UI 调过的风扇）
            all_fans = _enumerate_fans()          # 本机真实风扇全集（it87/nct）
            st = _load_fan_sys_temp()
            dt = _load_fan_disk_temp()
            sys_claimed, disk_claimed = _select_temp_fans(all_fans, st, dt)
            controlled = sys_claimed | disk_claimed
            controlling_any = False   # 本周期是否真正在写 PWM（接管 FCS 的依据）
            # 主板/CPU 温控（sys_temp）：优先级最高，先接管
            if st.get("enabled"):
                T = _fan_read_sys_temp(st.get("source", "cpu"))
                action, target = _fan_sys_temp_decision(T, st)
                for (hwmon, idx) in sys_claimed:
                    if action == "control" and target is not None:
                        _fan_smooth_step(hwmon, idx, target); controlling_any = True
                    elif action == "release":
                        _fan_release_auto(hwmon, idx)
                    # "hold" → 已交还自动，不再写入（避免与主板/内核抢控）
            # 硬盘温度控制（disk_temp）：接管未被 sys_temp 占用的受控风扇
            if dt.get("enabled") and dt.get("disks"):
                states = get_disk_temps(dt["disks"])
                action, target = _fan_disk_temp_decision(states, dt)
                for (hwmon, idx) in disk_claimed:
                    if action == "control" and target is not None:
                        _fan_smooth_step(hwmon, idx, target); controlling_any = True
                    elif action == "release":
                        _fan_release_auto(hwmon, idx)
                    # "hold" → 已交还自动，不再写入
            # 剩余风扇：仅处理用户在 UI 中手动/自动设过的（overrides）；未触碰的风扇保持原样（交还 BIOS/主板）
            for (hwmon, idx), cfg in overrides.items():
                if (hwmon, idx) in controlled:
                    continue  # 已被温控接管
                if cfg.get("mode") == "auto":
                    ct = _fan_cpu_temp_cached()
                    _fan_smooth_step(hwmon, idx, _fan_auto_pwm(ct)); controlling_any = True
                else:
                    tgt = cfg.get("target")
                    if tgt is None:
                        continue
                    _fan_smooth_step(hwmon, idx, tgt); controlling_any = True
            # 接管/交还系统风扇服务 FanControlServer：本应用真正控速任意风扇时停 FCS（避免抢控冲突），
            # 全部交还自动后重启 FCS 恢复 fnOS 原生控温。状态机保证只在边界切换时执行一次。
            if controlling_any and not _FCS_TAKEN["v"]:
                _fan_stop_ext_service(); _FCS_TAKEN["v"] = True
            elif not controlling_any and _FCS_TAKEN["v"]:
                _fan_start_ext_service(); _FCS_TAKEN["v"] = False
        except Exception:
            pass
        time.sleep(0.6)

_restore_fan_modes()   # 启动即恢复风扇模式（或首次默认接管自动控温），避免重启后全速
_fan_thread = _threading.Thread(target=fan_smooth_loop, daemon=True, name="fan-smooth")
_fan_thread.start()

# ===================== 风扇标注（用户可编辑名称/电压，按安装实例持久化）=====================
# 标注与硬件无关：只存 (hwmon, idx) -> {name, voltage}，不写死任何机型，对所有用户（含 IT87）安全。
FAN_LABELS_FILE = os.path.join(_config_dir(), "fan_labels.json")
_FAN_VOLT_ALLOWED = ("12V", "5V", "未知", "")

def _load_fan_labels():
    return _load_json_file(FAN_LABELS_FILE, {})

def _save_fan_labels(d):
    return _save_json_file(FAN_LABELS_FILE, d)

def _fan_label_for(hwmon, idx):
    labels = _load_fan_labels()
    lbl = labels.get(f"{hwmon}::{idx}")
    if not lbl:
        # hwmon 路径可能跨重启变化（如 hwmon4→hwmon3），按通道序号兜底命中，
        # 避免重启后标签/隐藏全部错位（用户按界面序号标的名字仍对得上）。
        for k, v in labels.items():
            if k.endswith(f"::{idx}"):
                lbl = v
                break
    return lbl or {}

# ===================== 风扇：硬盘温度控制（disk_temp）=====================
# 论坛需求（服务器/硬盘多/风扇多场景）：用指定硬盘温度驱动风扇——
# 如设置若干硬盘，40°C 开转、60°C 全速、硬盘休眠则停转。nasdash 增量支持，不替换现有 IT87/NCT 温控。
FAN_DISK_TEMP_FILE = os.path.join(_config_dir(), "fan_disk_temp.json")

def _load_fan_disk_temp():
    """读取硬盘温度控风扇配置（缺省关闭）。"""
    defaults = {
        "enabled": False,
        "disks": [],                 # 监控的硬盘 device，如 ["/dev/sda","/dev/sdb"]
        "start_temp": 40,            # 低于此温度 → 停转（开转阈值）；盘温重新 ≥ 此值才重新接管
        "full_temp": 60,             # 达到此温度 → 全速（max_pwm，默认 100=满转）
        "min_pwm": 30,               # 开转时最低占空比（%）
        "max_pwm": 100,              # 全速占空比（%）；full_temp 档即此值，默认 100=全速
        "recover_temp": 35,          # 盘温低于此值 → 受控风扇交还主板/内核自动控速（滞回，须 < start_temp）
        "sleep_stop": True,          # 所有监控盘休眠 → 风扇停转
        "controlled_fans": "all",    # "all" 或 [[hwmon,idx],...]
    }
    d = _load_json_file(FAN_DISK_TEMP_FILE, None)
    if isinstance(d, dict):
        for k in defaults:
            if k in d:
                defaults[k] = d[k]
    return defaults

def _save_fan_disk_temp(cfg):
    return _save_json_file(FAN_DISK_TEMP_FILE, cfg)

# ===================== 风扇：主板/CPU 温度控制（sys_temp）=====================
# 与 disk_temp 对称的另一套「温度曲线控速」：温度源来自 CPU 封装温度或主板温度，
# 同样用 start/full/recover/min/max + 受控风扇 的滞回曲线。两套互不干扰，可分别接管不同风扇
# （如 CPU 风扇交给 sys_temp 按 CPU 温度控，机箱风扇交给 disk_temp 按硬盘温度控）。
FAN_SYS_TEMP_FILE = os.path.join(_config_dir(), "fan_sys_temp.json")

def _load_fan_sys_temp():
    """读取主板/CPU 温度控风扇配置（缺省关闭）。"""
    defaults = {
        "enabled": False,
        "source": "cpu",             # cpu=CPU 封装温度(coretemp Package)；mb=主板温度(it87/nct systin)
        "start_temp": 45,            # 低于此温度 → 停转（开转阈值）；温度重新 ≥ 此值才重新接管
        "full_temp": 70,             # 达到此温度 → 全速（max_pwm，默认 100=满转）
        "min_pwm": 30,               # 开转时最低占空比（%）
        "max_pwm": 100,              # 全速占空比（%）；full_temp 档即此值，默认 100=全速
        "recover_temp": 40,          # 温度低于此值 → 受控风扇交还主板/内核自动控速（滞回，须 < start_temp）
        "controlled_fans": "all",    # "all" 或 [[hwmon,idx],...]
    }
    d = _load_json_file(FAN_SYS_TEMP_FILE, None)
    if isinstance(d, dict):
        for k in defaults:
            if k in d:
                defaults[k] = d[k]
    return defaults

def _save_fan_sys_temp(cfg):
    return _save_json_file(FAN_SYS_TEMP_FILE, cfg)

def _fan_read_sys_temp(source):
    """读取主板/CPU 温控的温度源单值（°C），自动适配不同主板传感器布局。
    source='cpu' → CPU 封装温度：优先 coretemp 的 'Package id' / AMD k10temp/zenpower 的 'Tdie'/'Tctl'；
                   找不到时回落取 coretemp 全部通道最高，再回落取所有传感器最高。
    source='mb'  → 主板温度：排除 coretemp 等 CPU 芯片后取最高（it87/nct/等主板传感器），
                   回落取所有传感器最高。
    同时兼容 sensors -j 的嵌套格式(chip->{标签:{tempN_input:值}})与扁平格式(chip->{tempN_input:值})，
    不依赖芯片具体型号，换主板/CPU 后仍能正确取温。读不到返回 None。"""
    source = (source or "cpu").lower()
    try:
        out = run_cmd(["sensors", "-j"], 5)
        j = json.loads(out)
        # temps: (chip, label, value) —— label 为传感器名(Package id 0 / Tdie / temp1_input 等)
        temps = []
        for chip, entries in j.items():
            if not isinstance(entries, dict):
                continue
            for _ename, fields in entries.items():
                if isinstance(fields, dict):
                    # 嵌套格式：chip -> { "Package id 0": {"temp1_input": 55.0}, ... }
                    for k, v in fields.items():
                        if k.startswith("temp") and k.endswith("_input"):
                            try:
                                temps.append((chip, _ename, float(v)))
                            except (TypeError, ValueError):
                                pass
                elif isinstance(fields, (int, float)) and _ename.startswith("temp") and _ename.endswith("_input"):
                    # 扁平格式：chip -> { "temp1_input": 40.0, ... }
                    try:
                        temps.append((chip, _ename, float(fields)))
                    except (TypeError, ValueError):
                        pass
        if temps:
            _prio = ("package", "tdie", "tctl")
            def _is_prio(t):
                return any(p in t[1].lower() for p in _prio)
            if source == "mb":
                mb = [t for t in temps if "coretemp" not in t[0].lower()]
                if mb:
                    return max(t[2] for t in mb)
                return max(t[2] for t in temps)  # 回落
            else:
                # CPU：优先 CPU 封装/Tdie/Tctl 标签，再回落 coretemp 全部通道，最后回落全部传感器
                prio = [t for t in temps if _is_prio(t)]
                if prio:
                    return max(t[2] for t in prio)
                cpu = [t for t in temps if "coretemp" in t[0].lower()]
                if cpu:
                    return max(t[2] for t in cpu)
                return max(t[2] for t in temps)  # 回落
    except Exception:
        pass
    return None

def _fan_curve_pwm(T, cfg, default_min, default_max):
    """自定义温度→PWM 曲线（分段线性）。cfg["curve"]=[[temp,pwm],...]（已按温度升序）。
    返回 raw(0~255) 或 None（曲线无效/缺失→交由调用方回退线性）。"""
    pts = cfg.get("curve")
    if not isinstance(pts, list) or len(pts) < 2:
        return None
    try:
        pts = sorted([(float(p[0]), float(p[1])) for p in pts if isinstance(p, (list, tuple)) and len(p) == 2], key=lambda x: x[0])
    except Exception:
        return None
    if not pts:
        return None
    if T is None:
        return None
    mn = float(cfg.get("min_pwm", default_min))
    mx = float(cfg.get("max_pwm", default_max))
    start = float(cfg.get("start_temp", pts[0][0]))
    if T < start:
        return 0
    if T >= pts[-1][0]:
        return round(min(max(pts[-1][1], mn), mx) / 100 * 255)
    for i in range(1, len(pts)):
        if T <= pts[i][0]:
            t0, p0 = pts[i-1]; t1, p1 = pts[i]
            frac = (T - t0) / (t1 - t0) if t1 > t0 else 0
            pw = p0 + frac * (p1 - p0)
            return round(min(max(pw, mn), mx) / 100 * 255)
    return round(min(max(pts[0][1], mn), mx) / 100 * 255)

def _fan_sys_temp_pwm(T, cfg):
    """按单值温度 T 算目标 raw(0~255)。优先自定义曲线；否则 start/full 线性。"""
    curve_raw = _fan_curve_pwm(T, cfg, 30, 100)
    if curve_raw is not None:
        return curve_raw
    start = float(cfg.get("start_temp", 45))
    full = float(cfg.get("full_temp", 70))
    minp = float(cfg.get("min_pwm", 30))
    maxp = float(cfg.get("max_pwm", 100))
    if T is None:
        return None
    if T < start:
        return 0
    if T >= full:
        return round(maxp / 100 * 255)
    r = (T - start) / (full - start)
    raw = minp + r * (maxp - minp)
    return round(raw / 100 * 255)

# 主板/CPU 温控滞回状态：None=未初始化, True=nasdash 接管控速, False=已交还主板自动
_st_engaged = {"v": None}

def _fan_sys_temp_decision(T, cfg):
    """主板/CPU 温控滞回状态机。返回 (action, pwm)：
      "control" → 按曲线接管，pwm 为目标 raw
      "release" → 温度低于 recover_temp（或读不到温度）→ 交还自动
      "hold"    → 已交还且温度仍在滞回区(recover≤T<start)→ 保持释放、不写
    滞回：接管后须 T<recover 才释放；释放后须 T≥start 才重新接管（避免临界抖动）。"""
    global _st_engaged
    start = float(cfg.get("start_temp", 45))
    recover = float(cfg.get("recover_temp", start - 5))
    if recover >= start:
        recover = start - 5  # 安全约束：recover 必须 < start
    if T is None:
        _st_engaged["v"] = False
        return ("release", None)
    if _st_engaged["v"] is None:
        _st_engaged["v"] = (T >= start)
    if _st_engaged["v"]:
        if T < recover:
            _st_engaged["v"] = False
            return ("release", None)
        return ("control", _fan_sys_temp_pwm(T, cfg))
    else:
        if T >= start:
            _st_engaged["v"] = True
            return ("control", _fan_sys_temp_pwm(T, cfg))
        return ("hold", None)

def get_disk_temps(devs):
    """读指定硬盘温度。sdX 用 smartctl -n standby（不唤醒休眠盘）；
    NVMe 不支持 -n standby，直接读温度（NVMe 一般不停机休眠）。
    返回 {dev: {"temp":int|None, "asleep":bool|None}}。"""
    states = {}
    for dev in devs or []:
        try:
            if dev.startswith("/dev/nvme"):
                out = sudo_cmd([SMARTCTL, "-A", dev], 8)
                asleep = False
            else:
                out = sudo_cmd([SMARTCTL, "-n", "standby", "-A", dev], 8)
                asleep = False
                if out and "STANDBY" in out.upper():
                    states[dev] = {"temp": None, "asleep": True}
                    continue
            if not out:
                states[dev] = {"temp": None, "asleep": None}
                continue
            temp = None
            for line in out.splitlines():
                if "Temperature_Celsius" in line or "Airflow_Temperature" in line:
                    m = re.search(r"-\s*(\d+)", line)
                    if m:
                        temp = int(m.group(1))
                        break
                elif "Temperature:" in line:
                    m = re.search(r"Temperature:\s*(\d+)", line)
                    if m:
                        t = int(m.group(1))
                        if t > 200:  # NVMe 偶报 Kelvin，转 Celsius
                            t = t - 273
                        temp = t
                        break
            states[dev] = {"temp": temp, "asleep": asleep}
        except Exception:
            states[dev] = {"temp": None, "asleep": None}
    return states

def _fan_disk_temp_pwm(states, cfg):
    """按硬盘温度算目标 raw(0~255)。
    - 所有监控盘休眠且 sleep_stop → 0（停转）
    - 优先自定义温度→PWM 曲线；否则取最热盘温度 T：T<start → 0；start≤T<full → min~max 线性；T≥full → max
    """
    sleep_stop = bool(cfg.get("sleep_stop", True))
    valid = [s for s in (states or {}).values() if isinstance(s, dict)]
    if not valid:
        return None
    if sleep_stop and all(s.get("asleep") for s in valid):
        return 0
    temps = [s["temp"] for s in valid if isinstance(s.get("temp"), (int, float))]
    if not temps:
        # 无温度读数：有曲线则以曲线最低点兜底，否则回退 min_pwm
        if cfg.get("curve"):
            return _fan_curve_pwm(None, cfg, 30, 70)
        return round(float(cfg.get("min_pwm", 30)) / 100 * 255)
    T = max(temps)
    curve_raw = _fan_curve_pwm(T, cfg, 30, 70)
    if curve_raw is not None:
        return curve_raw
    start = float(cfg.get("start_temp", 40))
    full = float(cfg.get("full_temp", 60))
    minp = float(cfg.get("min_pwm", 30))
    maxp = float(cfg.get("max_pwm", 70))
    if T < start:
        return 0
    if T >= full:
        return round(maxp / 100 * 255)
    r = (T - start) / (full - start)
    raw = minp + r * (maxp - minp)
    return round(raw / 100 * 255)

# 硬盘温控滞回状态：None=未初始化, True=nasdash 接管控速, False=已交还主板自动
_dt_engaged = {"v": None}

_FAN_ENABLE_CACHE = {}   # (hwmon, idx) -> 上次写入的 pwm_enable 值，避免每个 tick 重复写 sysfs
def _fan_set_enable(hwmon, idx, val):
    """设置 pwm_enable：1=软件接管控速（nasdash 写 pwm 生效）；2=交还主板/内核自动。带缓存，值不变则不写。"""
    key = (hwmon, idx)
    if _FAN_ENABLE_CACHE.get(key) == val:
        return True
    try:
        with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
            f.write(str(val))
        _FAN_ENABLE_CACHE[key] = val
        return True
    except Exception:
        return False

def _fan_release_auto(hwmon, idx):
    """把风扇交还主板/内核自动控速（pwm_enable=2）。FCS 若存在会重新接管。"""
    return _fan_set_enable(hwmon, idx, 2)

def _fan_disk_temp_decision(states, cfg):
    """硬盘温控滞回状态机。返回 (action, pwm)：
      "control" → 按曲线接管，pwm 为目标 raw
      "release" → 盘温低于 recover_temp（或休眠/读不到温度）→ 交还自动
      "hold"    → 已交还且盘温仍在滞回区(recover≤T<start)→ 保持释放、不写
    滞回：接管后须 T<recover 才释放；释放后须 T≥start 才重新接管（避免临界抖动）。"""
    global _dt_engaged
    start = float(cfg.get("start_temp", 40))
    recover = float(cfg.get("recover_temp", start - 5))
    if recover >= start:
        recover = start - 5  # 安全约束：recover 必须 < start
    valid = [s for s in (states or {}).values() if isinstance(s, dict)]
    if not valid:
        _dt_engaged["v"] = False
        return ("release", None)
    sleep_stop = bool(cfg.get("sleep_stop", True))
    if sleep_stop and all(s.get("asleep") for s in valid):
        _dt_engaged["v"] = False
        return ("release", None)
    temps = [s["temp"] for s in valid if isinstance(s.get("temp"), (int, float))]
    if not temps:
        _dt_engaged["v"] = False
        return ("release", None)
    T = max(temps)
    if _dt_engaged["v"] is None:
        _dt_engaged["v"] = (T >= start)
    if _dt_engaged["v"]:
        if T < recover:
            _dt_engaged["v"] = False
            return ("release", None)
        return ("control", _fan_disk_temp_pwm(states, cfg))
    else:
        if T >= start:
            _dt_engaged["v"] = True
            return ("control", _fan_disk_temp_pwm(states, cfg))
        return ("hold", None)

# ===================== 采集：阵列卡 =====================
def detect_storage_controllers():
    """用 lspci 检测存储控制器，区分 MegaRAID(IR) 与 HBA(IT) 直通卡"""
    out = run_cmd(["lspci", "-nn"], 10)
    controllers = []
    for line in out.splitlines():
        # 只按设备类型识别（RAID/SAS/SCSI/HBA），不限制厂商白名单，
        # 换任意品牌阵列卡/HBA（LSI/Broadcom/Areca/HighPoint/Adaptec/...）都能自动纳入
        if not re.search(r"(RAID|SAS|SCSI|HBA)", line, re.I):
            continue
        m = re.search(r":\s*(.+)$", line)
        model = m.group(1).strip() if m else line.strip()
        is_megaraid = bool(re.search(r"MegaRAID", line, re.I))
        # 含 SAS/HBA 但不含 MegaRAID → 视为 HBA 直通卡（IT 模式）
        is_hba = bool(re.search(r"SAS|HBA", line, re.I)) and not is_megaraid
        controllers.append({"model": model, "is_megaraid": is_megaraid, "is_hba": is_hba})
    return controllers

def _storcli_size_to_decimal(size_str):
    """storcli 把二进制 TiB/GiB 误标成 TB/GB，这里换算回十进制显示（如 6.366 TB -> 7.0T）"""
    try:
        m = re.match(r"^([\d.]+)\s*(TB|GB|MB)$", size_str.strip(), re.I)
        if not m:
            return size_str
        num = float(m.group(1))
        unit = m.group(2).upper()
        # storcli 实际是按二进制：TB=TiB(1024^4)、GB=GiB(1024^3)
        bytes_ = num * (1024 ** 4 if unit == "TB" else 1024 ** 3 if unit == "GB" else 1024 ** 2)
        tb = bytes_ / 1e12
        if tb >= 1:
            return f"{tb:.1f}T"
        gb = bytes_ / 1e9
        return f"{gb:.0f}G"
    except Exception:
        return size_str

def _resolve_brand_model(model, inquiry_model):
    """解析阵列卡物理盘用于品牌识别的型号。

    storcli 表格的 model 列常丢厂商前缀（如 SATA 盘只给 `SV300S37A/120G`，
    而 `KINGSTON` 在上一列）。若表格型号无厂商前缀、但 Inquiry Data / Model Number
    含前缀，则改用完整型号，避免把 `KINGSTON SV300S37A/120G` 误判为三星。
    详见 v1.7.8 品牌修复。行为保持与内联逻辑完全一致。
    """
    brand_model = model
    if inquiry_model and inquiry_model != "-":
        _known = ("ST", "WD", "WDC", "TOSHIBA", "HGST", "HUH", "HUS", "INTEL",
                  "KINGSTON", "CT", "CRUCIAL", "MICRON", "SANDISK", "PNY", "HITACHI", "SAMSUNG")
        _tbl_vendor = model.upper().startswith(_known) or "SAMSUNG" in model.upper()
        _inq_vendor = inquiry_model.upper().startswith(_known) or "SAMSUNG" in inquiry_model.upper()
        if (not _tbl_vendor) and _inq_vendor:
            brand_model = inquiry_model
    return brand_model


def disk_brand_and_feature(model):
    """根据型号解析硬盘品牌与特性（如双磁臂/双执行器）。返回 (brand_cn, feature)"""
    model_u = (model or "").strip().upper()
    # 品牌识别
    if model_u.startswith("ST"):
        brand = "希捷(Seagate)"
    elif model_u.startswith(("WD", "WDC")):
        brand = "西部数据(WD)"
    elif "TOSHIBA" in model_u:
        brand = "东芝(Toshiba)"
    elif model_u.startswith(("HGST", "HUH", "HUS")):
        brand = "HGST(日立)"
    elif "SAMSUNG" in model_u:
        brand = "三星(Samsung)"
    elif model_u.startswith("INTEL"):
        brand = "英特尔(Intel)"
    elif model_u.startswith("KINGSTON"):
        brand = "金士顿(Kingston)"
    elif model_u.startswith(("CT", "CRUCIAL")):
        brand = "英睿达(Crucial)"
    elif "MICRON" in model_u:
        brand = "美光(Micron)"
    elif "SANDISK" in model_u:
        brand = "闪迪(SanDisk)"
    elif model_u.startswith("PNY"):
        brand = "PNY"
    elif "HITACHI" in model_u:
        brand = "日立(Hitachi)"
    else:
        brand = ""
    # 双磁臂（双执行器）识别：已知 Seagate Exos 2X 系列
    dual_models = {"ST14000NM0001", "ST10000NM0096", "ST18000NM000J", "ST20000NM007D"}
    feature = "双磁臂(双执行器)" if model_u in dual_models else ""
    return brand, feature

@_ttl_cache(30)
def _smart_rpm_by_serial():
    """扫描所有 /dev/sdX，建立 {序列号(大写): 转速文本} 映射。
    storcli 不提供真实转速，必须用 smartctl -i 取 Rotation Rate（7200 rpm / 固态）。
    """
    rpm_map = {}
    try:
        devs = [d for d in os.listdir("/dev") if re.match(r"^sd[a-z]+$", d)]
    except Exception:
        return rpm_map
    for d in sorted(devs):
        try:
            out = sudo_cmd(["smartctl", "-i", "/dev/" + d], 8)
        except Exception:
            continue
        sn_m = re.search(r"Serial\s*(?:number|Number)\s*:\s*(\S+)", out)
        rpm_m = re.search(r"Rotation Rate:\s*(.+)", out)
        if not (sn_m and rpm_m):
            continue
        rpm = rpm_m.group(1).strip()
        if "Solid State" in rpm or "SSD" in rpm:
            rpm = "固态(SSD)"
        rpm_map[sn_m.group(1).strip().upper()] = rpm
    return rpm_map

def _parse_roc_temp(text):
    """从 storcli 输出中解析阵列卡芯片温度(ROC Temperature)，兼容多种格式。

    已验证兼容：
      - MegaRAID /c0 show :  "ROC temperature = 56"  / "Controller Temperature = 56"
      - HBA /c0 show temperature : "ROC temperature(Degree Celsius) 65" (无等号)
    """
    if not text:
        return None
    m = re.search(r"(?:Controller\s+Temperature|ROC\s+temperature\s*(?:\([^)]*\))?)\s*=?\s*(\d+)", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"ROC\s+temperature.*?(\d+)", text, re.I)  # 兜底：极宽松匹配 ROC 后第一个数字
    return int(m.group(1)) if m else None


@_ttl_cache(12)
def get_raid_card():
    data = {"ok": False, "mode": "none", "model": "未检测到",
            "drives": [], "raw": "", "note": "", "controllers": []}
    out = sudo_cmd([STORCLI, "/c0", "show"], 30)
    data["raw"] = out
    # ---- MegaRAID (IR 模式) ----
    if out and "Product Name" in out:
        def grab(pat, default=""):
            m = re.search(pat, out)
            return m.group(1).strip() if m else default
        data["ok"] = True
        data["mode"] = "mega"
        data["model"] = grab(r"Product Name\s*=\s*(.+)")
        data["serial"] = grab(r"Serial Number\s*=\s*(\S+)")
        data["sas_address"] = grab(r"SAS Address\s*=\s*(\S+)")
        data["fw_package"] = grab(r"FW Package Build\s*=\s*(\S+)")
        data["fw_version"] = grab(r"FW Version\s*=\s*(\S+)")
        data["bios_version"] = grab(r"BIOS Version\s*=\s*(\S+)")
        data["driver"] = grab(r"Driver Name\s*=\s*(\S+)") + " " + grab(r"Driver Version\s*=\s*(\S+)")
        data["pci"] = grab(r"PCI Address\s*=\s*(\S+)")
        data["jbod_count"] = grab(r"JBOD Drives\s*=\s*(\d+)", "0")
        # CacheVault
        cv = re.search(r"CVPM\w+\s+(\w+)\s+(\d+C)", out)
        if cv:
            data["cachevault"] = f"{cv.group(0).strip()}"
        else:
            data["cachevault"] = "未检测到"
        # 阵列卡芯片温度 (ROC Temperature)，兼容多种 storcli 输出格式
        temp = _parse_roc_temp(out)
        if temp is None:
            temp = _parse_roc_temp(sudo_cmd([STORCLI, "/c0", "show", "all"], 15))
        if temp is None:
            # LSI-9300 等 HBA 卡 /c0 show 不含温度，必须单独跑 /c0 show temperature
            temp = _parse_roc_temp(sudo_cmd([STORCLI, "/c0", "show", "temperature"], 10))
        data["controller_temp"] = temp
        # 物理盘列表（用 split 解析表格行，更健壮）
        # 格式: 252:0 21 JBOD - 6.366 TB SAS HDD N N 4 KB ST14000NM0001 U -
        rpm_map = _smart_rpm_by_serial()
        drives = []
        seen_slots = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 12 and re.match(r"^\d+:\d+$", parts[0]):
                if parts[0] in seen_slots:
                    continue
                seen_slots.add(parts[0])
                # 先从 /c0 show 行取 model（标准 MegaRAID 格式）
                model = parts[12] if len(parts) > 12 else ""
                # 用每张盘的序列号匹配 smartctl 真实转速（storcli 不提供 RPM）
                e, s = parts[0].split(":")
                sn = ""
                inquiry_model = ""
                try:
                    sn_out = sudo_cmd([STORCLI, "/c0", "/e" + str(e), "/s" + str(s), "show", "all"], 15)
                    sn_m = re.search(r"SN\s*=\s*(\S+)", sn_out)
                    if sn_m:
                        sn = sn_m.group(1).strip()
                    # 某些卡/扩展器下 /c0 show 的 model 列显示 "-"，从 show all 取更准的型号兜底
                    m = re.search(r"Model Number\s*=\s*(.+)", sn_out) or re.search(r"Inquiry Data\s*=\s*(.+)", sn_out)
                    if m:
                        inquiry_model = " ".join(m.group(1).strip().split())
                except Exception:
                    pass
                # 表格列的型号常丢厂商前缀（如 SATA 盘只给 SV300S37A/120G，KINGSTON 在上一列），
                # 直接用该型号做品牌识别会被误判（如 SV 开头误认三星）。
                # 故品牌识别优先用含厂商前缀的完整型号（Model Number / Inquiry Data）。
                brand_model = _resolve_brand_model(model, inquiry_model)
                brand, feature = disk_brand_and_feature(brand_model)
                # 展示用 model：表格列已够用则保留（与 HDD 显示风格一致），仅在表格缺失时兜底用完整型号
                if (not model or model == "-") and inquiry_model and inquiry_model != "-":
                    model = inquiry_model
                rpm = rpm_map.get(sn.upper(), "")
                if not rpm:
                    rpm = "固态(SSD)" if parts[7].upper() == "SSD" else "—"
                size_dec = _storcli_size_to_decimal(parts[4] + " " + parts[5])
                size_note = ""
                if feature and "双磁臂" in feature:
                    # 双磁臂盘每块执行器向系统暴露一半容量，整盘为 2×；显示整盘标称容量
                    m = re.match(r"^([\d.]+)\s*([TG])$", size_dec)
                    if m:
                        full = float(m.group(1)) * 2
                        size_dec = f"{full:.1f}{m.group(2)}"
                        size_note = f"双磁臂·整盘{size_dec}（每执行器 {(full/2):.1f}{m.group(2)}）"
                drives.append({
                    "slot": parts[0], "did": parts[1], "state": parts[2],
                    "dg": parts[3], "size": size_dec, "size_note": size_note,
                    "intf": parts[6], "media": parts[7],
                    "model": model, "sp": parts[13] if len(parts) > 13 else "",
                    "sn": sn, "rpm": rpm,
                    "brand": brand, "feature": feature,
                })
        data["drives"] = drives
        return data
    # ---- 非 MegaRAID：判断是否为 HBA 直通卡 / 纯 SATA ----
    controllers = detect_storage_controllers()
    data["controllers"] = controllers
    hba = [c for c in controllers if c["is_hba"]]
    megaraid = [c for c in controllers if c["is_megaraid"]]
    if hba:
        data["ok"] = True
        data["mode"] = "hba"
        data["model"] = hba[0]["model"]
        # HBA 直通卡芯片温度：/c0 show 不含温度，需单独跑 /c0 show temperature
        try:
            data["controller_temp"] = _parse_roc_temp(sudo_cmd([STORCLI, "/c0", "show", "temperature"], 10)) \
                or _parse_roc_temp(sudo_cmd([STORCLI, "/c0", "show"], 30))
        except Exception:
            data["controller_temp"] = None
        data["note"] = ("HBA 直通卡（IT 模式）：磁盘由系统内核直接管理，不经阵列卡固件。"
                        "HBA 芯片本身无独立温度传感器，本页不显示阵列卡温度（属正常现象，并非面板异常）。"
                        "每张物理盘的温度与 SMART 信息请见「硬盘 SMART」标签页。")
        return data
    if megaraid:
        data["ok"] = False
        data["mode"] = "mega_error"
        data["model"] = megaraid[0]["model"]
        data["note"] = "检测到 MegaRAID 卡，但 storcli 读取失败，请确认已安装 storcli 且本应用具备 sudo 权限。"
        return data
    data["ok"] = False
    data["mode"] = "none"
    data["note"] = "未检测到独立阵列卡 / HBA（纯 SATA 主板，磁盘由南桥直接管理）。"
    return data

# ===================== 采集：硬盘 SMART =====================
def parse_sas_smart(text):
    """解析 SAS/SCSI 盘 SMART"""
    d = {}
    m = re.search(r"SMART Health Status:\s*(\w+)", text)
    d["health"] = m.group(1) if m else "UNKNOWN"
    m = re.search(r"Current Drive Temperature:\s*(\d+)\s*C", text)
    d["temp"] = int(m.group(1)) if m else None
    m = re.search(r"Drive Trip Temperature:\s*(\d+)\s*C", text)
    d["temp_trip"] = int(m.group(1)) if m else 60
    m = re.search(r"Accumulated power on time, hours:minutes\s*(\d+):(\d+)", text)
    d["power_on_hours"] = int(m.group(1)) if m else None
    m = re.search(r"Elements in grown defect list:\s*(\d+)", text)
    d["defects"] = int(m.group(1)) if m else 0
    m = re.search(r"Pending defect count:\s*(\d+)", text)
    d["pending"] = int(m.group(1)) if m else 0
    m = re.search(r"Non-medium error count:\s*(\d+)", text)
    d["non_medium_errors"] = int(m.group(1)) if m else 0
    # 错误计数表
    rm = re.search(r"read:.*?(\d+)\s*$", text, re.M)
    wm = re.search(r"write:.*?(\d+)\s*$", text, re.M)
    # 更稳妥地抓 uncorrected
    read_line = re.search(r"read:.*?(\d+)\s+(\d+)$", text, re.M)
    d["read_errors"] = read_line.group(2) if read_line else "0"
    write_line = re.search(r"write:.*?(\d+)\s+(\d+)$", text, re.M)
    d["write_errors"] = write_line.group(2) if write_line else "0"
    return d

def parse_ata_smart(text):
    """解析 ATA/SATA 盘 SMART"""
    d = {}
    m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", text)
    d["health"] = m.group(1) if m else "UNKNOWN"
    d["temp"] = None
    d["power_on_hours"] = None
    d["reallocated"] = 0
    d["pending"] = 0
    d["uncorrectable"] = 0
    d["udma_crc"] = 0
    d["raw_read_errors"] = "0"
    attrs = {}
    for line in text.splitlines():
        # 格式: ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+0x\w+\s+(\d+)\s+(\d+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+?)\s*$", line)
        if m:
            aid, name = m.group(1), m.group(2)
            val, worst, thresh = m.group(3), m.group(4), m.group(5)
            raw_str = m.group(6)
            # raw_value 取第一个数字（温度等可能是 "41 (Min/Max -1/56)"；某些版本 smartctl 会对大数加逗号）
            num_m = re.match(r"\s*([\d,]+)", raw_str)
            raw_num = int(num_m.group(1).replace(",", "")) if num_m else 0
            attrs[aid] = {"name": name, "value": val, "worst": worst, "thresh": thresh, "raw": raw_str.strip()}
            if aid == "9":
                d["power_on_hours"] = raw_num
            elif aid == "194":
                d["temp"] = raw_num
            elif aid == "5":
                d["reallocated"] = raw_num
            elif aid == "197":
                d["pending"] = raw_num
            elif aid == "198":
                d["uncorrectable"] = raw_num
            elif aid == "199":
                d["udma_crc"] = raw_num
            elif aid == "1":
                d["raw_read_errors"] = raw_str.strip()
    d["attrs"] = attrs
    return d

def parse_nvme_smart(text):
    """解析 NVMe 盘 SMART（smartctl -a /dev/nvmeXnY）"""
    d = {}
    m = re.search(r"SMART overall-health self-assessment test result:\s*(\w+)", text)
    d["health"] = m.group(1) if m else "UNKNOWN"
    d["temp"] = None
    d["power_on_hours"] = None
    d["percentage_used"] = None
    d["available_spare"] = None
    d["critical_warning"] = "0"
    d["data_units_read"] = None
    d["data_units_written"] = None
    # 温度：NVMe 可能报 Kelvin（>200 视为开尔文转摄氏）或 Celsius
    m = re.search(r"Temperature:\s*(\d+)\s*Kelvin", text)
    if m:
        d["temp"] = int(m.group(1)) - 273
    else:
        m = re.search(r"Temperature:\s*(\d+)\s*Celsius", text)
        if m:
            d["temp"] = int(m.group(1))
    m = re.search(r"Power On Hours:\s*([\d,]+)", text)
    d["power_on_hours"] = int(m.group(1).replace(",", "")) if m else None
    m = re.search(r"Percentage Used:\s*(\d+)%", text)
    d["percentage_used"] = int(m.group(1)) if m else None
    m = re.search(r"Available Spare:\s*(\d+)%", text)
    d["available_spare"] = int(m.group(1)) if m else None
    m = re.search(r"Critical Warning:\s*0x([0-9a-fA-F]+)", text)
    d["critical_warning"] = m.group(1) if m else "0"
    m = re.search(r"Data Units Read:\s*([\d,]+)(\s*\[[^\]]*\])?", text)
    d["data_units_read"] = (m.group(1) + (m.group(2) or "")).strip() if m else None
    m = re.search(r"Data Units Written:\s*([\d,]+)(\s*\[[^\]]*\])?", text)
    d["data_units_written"] = (m.group(1) + (m.group(2) or "")).strip() if m else None
    return d

@_ttl_cache(12)
def get_disks():
    """采集所有块设备 + SMART（SD/SAS 用 ls /dev/sd*，NVMe 用 ls /dev/nvme*；再用正则过滤掉分区/控制器，
    支持多位盘名 sdaa/sdab 与多控制器 nvme10n1 等；smartctl 拿详情，不依赖 lsblk 字段对齐）"""
    disks = []
    out = "\n".join(glob.glob("/dev/sd*"))
    devnames = sorted(set(l.strip().split('/')[-1] for l in out.split()
                          if l.strip() and re.match(r"^sd[a-z]+$", l.strip().split('/')[-1])))
    # NVMe 命名空间（如 /dev/nvme0n1；控制器 /dev/nvme0 不匹配 n\d+，不会误纳入）
    nvme_out = "\n".join(glob.glob("/dev/nvme*"))
    for l in nvme_out.split():
        n = l.strip().split('/')[-1]
        if re.match(r"^nvme\d+n\d+$", n):
            devnames.append(n)
    devnames = sorted(set(devnames))
    # lsblk 补充容量/rota/tran（-n 不打印表头，但仍防御性跳过首行若为表头）
    lsblk = run_cmd(["lsblk", "-dn", "-b", "-o", "NAME,SIZE,ROTA,TRAN"], 5)
    linfo = {}
    for line in lsblk.strip().splitlines():
        p = line.split()
        if not p or p[0].upper() == "NAME":
            continue
        if len(p) >= 2:
            linfo[p[0]] = {"size_b": p[1], "rota": p[2] if len(p) > 2 else "?",
                           "tran": p[3] if len(p) > 3 else ""}
    # 真实转速：smartctl -i 的 Rotation Rate（覆盖 ATA/SAS 机械盘；SSD 标“固态(SSD)”）
    rpm_map = _smart_rpm_by_serial()
    for name in devnames:
        dev = f"/dev/{name}"
        info = linfo.get(name, {})
        size_b = info.get("size_b", "0")
        try:
            gb = int(size_b) / 1e9
            size_str = f"{gb/1000:.1f}T" if gb >= 1000 else f"{gb:.0f}G"
        except:
            size_str = "?"
        smart_out = sudo_cmd([SMARTCTL, "-n", "standby", "-a", dev], 20)
        disk = {
            "dev": name, "size": size_str, "rota": info.get("rota", "?"),
            "model": "", "serial": "", "tran": info.get("tran", ""), "vendor": "",
            "type": "ata", "health": "N/A", "health_ok": False,
            "temp": None, "power_on_hours": None,
        }
        if smart_out:
            if "SMART Health Status" in smart_out:
                disk["type"] = "sas"
                disk.update(parse_sas_smart(smart_out))
                m = re.search(r"Vendor:\s*(.+)", smart_out)
                disk["vendor"] = m.group(1).strip() if m else ""
                m = re.search(r"Product:\s*(.+)", smart_out)
                disk["model"] = m.group(1).strip() if m else ""
                m = re.search(r"Serial number:\s*(\S+)", smart_out)
                disk["serial"] = m.group(1) if m else ""
            elif "SMART/Health Information" in smart_out or ("Model Number:" in smart_out and "Namespace" in smart_out):
                disk["type"] = "nvme"
                disk["tran"] = "nvme"
                disk["rota"] = "0"
                disk.update(parse_nvme_smart(smart_out))
                m = re.search(r"Model Number:\s*(.+)", smart_out)
                disk["model"] = m.group(1).strip() if m else ""
                m = re.search(r"Serial Number:\s*(\S+)", smart_out)
                disk["serial"] = m.group(1) if m else ""
            elif "overall-health" in smart_out:
                disk["type"] = "ata"
                disk.update(parse_ata_smart(smart_out))
                m = re.search(r"Device Model:\s*(.+)", smart_out)
                if not m:
                    m = re.search(r"Model Family:\s*(.+)", smart_out)
                disk["model"] = m.group(1).strip() if m else ""
                m = re.search(r"Serial Number:\s*(\S+)", smart_out)
                disk["serial"] = m.group(1) if m else ""
        # 容量兜底：lsblk 返回 0 / 缺失 / 解析失败时，从 smartctl 取容量
        # ATA/SAS 用 "User Capacity"，NVMe 用 "Namespace 1 Size/Capacity" / "Total NVM Capacity"
        if smart_out and (disk["size"] in ("0G", "0.0G", "?", "0") or info.get("size_b") in ("0", "")):
            m = re.search(r"User Capacity:\s*([\d,]+)\s*bytes", smart_out)
            if not m:
                m = re.search(r"Namespace 1 Size/Capacity:\s*([\d,]+)", smart_out)
            if not m:
                m = re.search(r"Total NVM Capacity:\s*([\d,]+)", smart_out)
            if m:
                cap = int(m.group(1).replace(",", ""))
                gb = cap / 1e9
                disk["size"] = f"{gb/1000:.1f}T" if gb >= 1000 else f"{gb:.0f}G"
        b, f = disk_brand_and_feature(disk["model"])
        disk["brand"] = b
        disk["feature"] = f
        disk["rpm"] = rpm_map.get(disk.get("serial", "").upper(), "") if disk.get("serial") else ""
        # 类型兜底：lsblk ROTA 不可靠时，用 smartctl Rotation Rate 覆盖 HDD/SSD
        if disk.get("rpm"):
            if disk["rpm"] == "固态(SSD)":
                disk["rota"] = "0"
            elif "rpm" in disk["rpm"].lower():
                disk["rota"] = "1"
        disk["health_ok"] = disk["health"].upper() in ("OK", "PASSED")
        disks.append(disk)
    return disks

# ===================== 采集：系统资源 =====================
# ===================== 采集：主板 / 内存（dmidecode） =====================
def _mem_brand_cn(manu):
    """把内存条制造商英文字符串映射为中文品牌（未知原样返回）"""
    if not manu:
        return ""
    m = manu.strip().upper()
    table = [
        ("SAMSUNG", "三星"), ("SK HYNIX", "海力士"), ("HYNIX", "海力士"),
        ("KINGSTON", "金士顿"), ("MICRON", "美光"), ("CRUCIAL", "英睿达"),
        ("CORSAIR", "海盗船"), ("G.SKILL", "芝奇"), ("G SKILL", "芝奇"),
        ("KINGMAX", "宇瞻"), ("ADATA", "威刚"), ("APACER", "宇瞻"),
        ("TRANSCEND", "创见"), ("TEAM", "十铨"), ("WESTERN", "西数"),
        ("WD", "西数"), ("INTEL", "英特尔"), ("RAMAXEL", "记忆科技"),
        ("ELPIDA", "尔必达"), ("NANYA", "南亚"),
        ("GALAXY MICROSYSTEMS", "影驰"), ("GALAX", "影驰"),
    ]
    for key, cn in table:
        if key in m:
            return cn
    # JEDEC 十六进制厂商码（SPD 仅含厂商码、无可读品牌名时 dmidecode 输出）
    hex_table = {
        "CE": "三星", "04E8": "三星",
        "AD": "海力士", "04D5": "海力士",
        "2C": "美光", "2D": "美光", "FF": "美光",
        "98": "金士顿", "04": "金士顿",
        "8892": "影驰(GALAX)", "8922": "影驰(GALAX)",
    }
    if re.fullmatch(r"[0-9A-Fa-f]+", manu.strip()):
        code = manu.strip().upper()
        return hex_table.get(code, f"未知(厂商码0x{code})")
    return manu.strip()


def get_chipset():
    """用 lspci 的 Host bridge 设备 ID 推断 Intel 芯片组系列（飞牛未带 pciids 数据库时用 Device ID 匹配）"""
    out = sudo_cmd(["lspci"], 5)
    hid = ""
    for line in out.splitlines():
        if "Host bridge" in line:
            m = re.search(r"Device ([0-9a-fA-F]{4})", line)
            if m:
                hid = m.group(1).lower()
                break
    if not hid:
        return ""
    table = {
        "190f": "100/200 系列（6/7代酷睿）", "1910": "100 系列", "1900": "100 系列（如 H110/B150）",
        "590f": "200 系列（7代）", "5910": "200 系列",
        "3e0f": "300 系列（8/9代酷睿）", "3ec2": "300 系列", "3e30": "300 系列", "3e31": "300 系列", "3e35": "300 系列",
        "9b00": "400 系列（10代）", "9b41": "400 系列",
        "4600": "600 系列（12代）", "4601": "600 系列", "4610": "600 系列",
        "7900": "700 系列（13代）", "7a00": "700 系列", "7d00": "700 系列",
        "a700": "800 系列（14代）", "a780": "800 系列",
    }
    return "Intel " + table.get(hid, f"未知芯片组（Host bridge 0x{hid}）")


@_ttl_cache(60)
def get_board():
    """主板信息：优先 /sys/class/dmi/id（免 root），空则 dmidecode；
    DMI 全空（准系统/工控白牌板常见）时尝试读取手动标注，最后回退芯片组识别。"""
    def _read_dmi_sysfs(name):
        v = read_file(f"/sys/class/dmi/id/{name}").strip()
        # fnOS 对这些字段固定返回 "Default string" / "To be filled by O.E.M." 等占位符
        if v.lower() in ("", "default string", "to be filled by o.e.m.", "not specified", "unknown"):
            return ""
        return v

    manufacturer = _read_dmi_sysfs("board_vendor")
    product = _read_dmi_sysfs("board_name")
    version = _read_dmi_sysfs("board_version")
    bios_vendor = _read_dmi_sysfs("bios_vendor")
    bios_version = _read_dmi_sysfs("bios_version")
    bios_date = _read_dmi_sysfs("bios_date")

    # sysfs 拿不到时再退 dmidecode（需 root）
    if not manufacturer or not product:
        out = sudo_cmd([DMIDECODE, "-t", "2"], 8)
        if not out:
            out = sudo_cmd([DMIDECODE, "-t", "1"], 8)
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Manufacturer:") and not manufacturer:
                manufacturer = s.split(":", 1)[1].strip()
            elif s.startswith("Product Name:") and not product:
                product = s.split(":", 1)[1].strip()
            elif s.startswith("Version:") and not version:
                version = s.split(":", 1)[1].strip()
        if manufacturer.lower() in ("default string", "to be filled by o.e.m.", "not specified"):
            manufacturer = ""
        if product.lower() in ("default string", "to be filled by o.e.m.", "not specified"):
            product = ""

    b = {
        "manufacturer": manufacturer,
        "product": product,
        "version": version,
        "bios_vendor": bios_vendor,
        "bios_version": bios_version,
        "bios_date": bios_date,
        "chipset": "",
        "override": False,
        "note": "",
    }

    # DMI 未写入厂商信息：先看手动标注文件，否则用 lspci 推断芯片组
    if not b["manufacturer"] or not b["product"]:
        override = ""
        try:
            with open(BOARD_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                override = f.read().strip()
        except Exception:
            override = ""
        if override:
            b["product"] = override
            b["override"] = True
            b["note"] = "手动标注（BIOS 未写入主板信息）"
        else:
            b["note"] = "BIOS 未写入主板厂商/型号（DMI 为空），以下为芯片组推断"
    # 芯片组：始终用 lspci 推断（大牌主板也能显示，更准确）
    b["chipset"] = get_chipset()
    return b


def _clean_mfr(s):
    """清理 decode-dimms 的厂商名（去掉 '? (Invalid parity)' 等后缀）"""
    s = s.strip()
    s = re.sub(r"\?.*$", "", s).strip()      # 去掉问号及之后
    s = re.sub(r"\(.*?\)", "", s).strip()     # 去掉括号内容
    return s


def get_memory_from_decodedimms():
    """用 decode-dimms 直读 SPD（JEP106 解码），拿到权威的模组厂/颗粒厂/型号/频率。
    仅在 i2c-tools 已安装且 SPD 可读时返回非空列表。"""
    dd = (shutil.which("decode-dimms") or "")
    if not dd:
        return []
    out = sudo_cmd([dd], 15)
    if not out:
        return []
    mods = []
    cur = {}
    def flush(c):
        if c.get("size_gb"):
            mods.append(c)
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Guessing DIMM") or s.startswith("Decoding EEPROM") or s.startswith("Memory Serial Presence Detect"):
            flush(cur)
            cur = {}
            continue
        # decode-dimms 用「字段名<多个空格>值」的固定列格式，按 2+ 空格切分
        parts = re.split(r"\s{2,}", s, 1)
        if len(parts) != 2:
            continue
        k, v = parts[0].strip(), parts[1].strip()
        if k == "Fundamental Memory type":
            cur["type"] = v
        elif k == "Module Type":
            cur["module_type"] = v
        elif k == "Maximum module speed":
            cur["speed"] = v.split("(")[0].strip()
        elif k == "Size":
            mb = re.search(r"(\d+)\s*MB", v, re.I)
            if mb:
                cur["size"] = v
                cur["size_gb"] = int(mb.group(1)) / 1024
            else:
                gb = re.search(r"(\d+)\s*GB", v, re.I)
                if gb:
                    cur["size"] = v
                    cur["size_gb"] = int(gb.group(1))
        elif k == "Module Manufacturer":
            cur["module_mfr"] = _clean_mfr(v)
        elif k == "DRAM Manufacturer":
            cur["dram_mfr"] = _clean_mfr(v)
        elif k == "Part Number":
            cur["part"] = "" if v.lower() in ("undefined", "none", "-") else v
    flush(cur)
    # 编号 + 品牌中文
    for i, m in enumerate(mods, 1):
        m["locator"] = f"DIMM{i}"
        mod_cn = _mem_brand_cn(m.get("module_mfr", ""))
        dram_cn = _mem_brand_cn(m.get("dram_mfr", ""))
        m["brand"] = mod_cn or dram_cn
        m["manufacturer"] = m.get("module_mfr", "")
        m["dram_manufacturer"] = dram_cn
    return mods


@_ttl_cache(60)
def get_memory_modules():
    """内存插槽信息：用 dmidecode -t 17 枚举所有物理插槽（含空），
    已安装槽再用 decode-dimms（SPD 直读）补权威品牌/颗粒厂。
    返回 slots(总插槽数)/installed(已装数)/empty(空槽数)/modules(含空槽)。"""
    def _size_gb(sz):
        mb = re.search(r"(\d+)\s*MB", sz, re.I)
        if mb:
            return int(mb.group(1)) / 1024
        gb = re.search(r"(\d+)\s*GB", sz, re.I)
        if gb:
            return int(gb.group(1))
        return 0

    # 1) decode-dimms 先拿已安装槽的权威 SPD 品牌（按 DIMM 顺序）
    spd_mods = get_memory_from_decodedimms()
    spd_by_idx = {i: m for i, m in enumerate(spd_mods)}

    # 2) dmidecode -t 17 枚举全部物理插槽（含空）
    out = sudo_cmd([DMIDECODE, "-t", "17"], 12)
    slots_raw = []
    cur = {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Memory Device"):
            if cur:
                slots_raw.append(cur)
            cur = {}
            continue
        if ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "Locator":
            cur["locator"] = v
        elif k == "Bank Locator":
            cur["bank"] = v
        elif k == "Manufacturer":
            cur["manufacturer"] = v
        elif k == "Part Number":
            cur["part"] = v
        elif k == "Size":
            cur["size"] = v
        elif k == "Type":
            cur["type"] = v
        elif k == "Serial Number":
            cur["serial"] = v
        elif k == "Speed":
            cur["speed"] = v
        elif k == "Configured Memory Speed":
            cur["cfg_speed"] = v
    if cur:
        slots_raw.append(cur)

    # 3) 组装：空槽标记 installed=False；已安装槽优先用 SPD 品牌
    modules = []
    spd_i = 0
    total_gb = 0
    if slots_raw:
        for idx, slot in enumerate(slots_raw):
            sz = (slot.get("size") or "").strip()
            installed = _size_gb(sz) > 0
            if installed:
                spd = spd_by_idx.get(spd_i)
                spd_i += 1
                mgb = _size_gb(sz)
                total_gb += mgb
                modules.append({
                    "locator": slot.get("locator") or slot.get("bank", f"DIMM{idx}"),
                    "installed": True,
                    "brand": (spd.get("brand") if spd else "") or _mem_brand_cn(slot.get("manufacturer", "")),
                    "manufacturer": (spd.get("manufacturer") if spd else slot.get("manufacturer", "")),
                    "dram_manufacturer": (spd.get("dram_manufacturer") if spd else ""),
                    "part": spd.get("part") if spd else slot.get("part", ""),
                    "size": sz,
                    "size_gb": mgb,
                    "type": (spd.get("type") if spd else slot.get("type", "")),
                    "speed": (spd.get("speed") if spd else (slot.get("cfg_speed") or slot.get("speed", ""))),
                    "serial": slot.get("serial", ""),
                    "source": "spd" if spd else "dmidecode",
                })
            else:
                modules.append({
                    "locator": slot.get("locator") or slot.get("bank", f"DIMM{idx}"),
                    "installed": False,
                    "brand": "",
                    "manufacturer": "",
                    "dram_manufacturer": "",
                    "part": "",
                    "size": "空",
                    "size_gb": 0,
                    "type": "",
                    "speed": "",
                    "serial": "",
                    "source": "empty",
                })
    else:
        # dmidecode 不可用：仅 SPD 已安装槽（无法枚举空槽）
        for idx, m in enumerate(spd_mods):
            mgb = m.get("size_gb", 0)
            total_gb += mgb
            modules.append({
                "locator": m.get("locator", f"DIMM{idx}"),
                "installed": True,
                "brand": m.get("brand", ""),
                "manufacturer": m.get("manufacturer", ""),
                "dram_manufacturer": m.get("dram_manufacturer", ""),
                "part": m.get("part", ""),
                "size": m.get("size", ""),
                "size_gb": mgb,
                "type": m.get("type", ""),
                "speed": m.get("speed", ""),
                "serial": "",
                "source": "spd",
            })

    slots = len(modules)
    installed_n = sum(1 for m in modules if m["installed"])
    empty_n = slots - installed_n
    brands = {}
    for m in modules:
        if m["brand"]:
            brands[m["brand"]] = brands.get(m["brand"], 0) + 1
    return {
        "modules": modules,
        "total_gb": total_gb,
        "slots": slots,
        "installed": installed_n,
        "empty": empty_n,
        "brand_summary": ", ".join(f"{k}×{v}" for k, v in brands.items()) or "未知",
    }


def get_system():
    d = {}
    d["hostname"] = socket.gethostname()
    d["kernel"] = platform.release()
    d["os"] = "Debian 12 (bookworm) / fnOS"
    # CPU
    lscpu = run_cmd(["lscpu"], 5)
    m = re.search(r"Model name:\s*(.+)", lscpu)
    d["cpu_model"] = m.group(1).strip() if m else "?"
    m = re.search(r"CPU\(s\):\s*(\d+)", lscpu)
    d["cpu_threads"] = int(m.group(1)) if m else 0
    m = re.search(r"Core\(s\) per socket:\s*(\d+)", lscpu)
    d["cpu_cores"] = int(m.group(1)) if m else 0
    m = re.search(r"CPU max MHz:\s*([\d.]+)", lscpu)
    d["cpu_freq"] = m.group(1) if m else "?"
    # 负载
    la = read_file("/proc/loadavg").split()
    d["load"] = la[:3] if len(la) >= 3 else ["0","0","0"]
    # uptime
    up = read_file("/proc/uptime").split()
    try:
        up_s = float(up[0])
        d["uptime"] = format_uptime(up_s)
    except:
        d["uptime"] = "?"
    # 内存
    meminfo = read_file("/proc/meminfo")
    mi = {}
    for line in meminfo.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            mi[m.group(1)] = int(m.group(2))
    mt = mi.get("MemTotal", 0); ma = mi.get("MemAvailable", 0)
    used = mt - ma
    d["memory"] = {
        "total": fmt_kb(mt), "used": fmt_kb(used), "available": fmt_kb(ma),
        "percent": round(used / mt * 100, 1) if mt else 0,
    }
    st = mi.get("SwapTotal", 0); sf = mi.get("SwapFree", 0)
    d["swap"] = {"total": fmt_kb(st), "used": fmt_kb(st - sf)}
    # 传感器分类解析（温度/风扇/电压）
    sens_j = run_cmd([SENSORS, "-j"], 8)
    d["sensors"] = {"temps": [], "fans": [], "voltages": []}
    cpu_temp = None
    # 风扇控制信息：优先系统风扇服务配置，其次 sysfs（不依赖任何外部应用）
    fan_info = {}
    # 1) 系统风扇服务配置（可选）—— 提供风扇名称/模式，并借 pwm_path 推断可写路径
    fc_raw = read_file("/vol2/@appconf/FanControlServer/config.json")
    if fc_raw:
        try:
            fc = json.loads(fc_raw)
            for fan in fc.get("fans", []):
                idx = fan.get("pwm_index")
                if not idx:
                    continue
                _hw = ""
                _ix = idx
                m = re.search(r"(/sys/class/hwmon/hwmon\d+)/pwm(\d+)", fan.get("pwm_path") or "")
                if m:
                    _hw = m.group(1)
                    _ix = int(m.group(2))
                fan_info[f"fan{idx}"] = {
                    "name": fan.get("name", f"风扇{idx}"),
                    "mode": fan.get("mode", ""),
                    "hwmon": _hw,
                    "idx": _ix,
                    "controllable": bool(_hw),
                }
        except (json.JSONDecodeError, ValueError):
            pass
    # 2) sysfs hwmon —— 不依赖芯片型号（it87/nct/fintek/winbond/asus 等皆可），
    #    枚举所有 hwmon 的 pwmN_enable 可写通道；遍历所有芯片、fan1-10，避免漏掉多芯片/集线器。
    #    复用 _enumerate_fans 保证与温控循环 / 风扇状态接口看到的风扇全集一致。
    for (_hw, _fi) in _enumerate_fans():
        _fk = f"fan{_fi}"
        _pe = read_file(f"{_hw}/pwm{_fi}_enable").strip()
        _pv = read_file(f"{_hw}/pwm{_fi}").strip()
        _controllable = bool(_pe)
        if _fk in fan_info:
            # 已知的风扇（来自系统风扇服务配置）：name/mode 用配置，
            # 但 hwmon/idx 一律以「实时枚举结果」为权威(优先级最高)。
            # 配置里写死的 pwm_path(如 hwmon3)会随内核重排失效，若仍优先用它，
            # GUI 滑块会拿到错误 hwmon → 调速请求命中不到真实通道(停在自动曲线35%)。
            fan_info[_fk]["hwmon"] = _hw
            fan_info[_fk]["idx"] = _fi
            fan_info[_fk]["controllable"] = fan_info[_fk].get("controllable") or _controllable
        else:
            # 仅 sysfs 暴露的风扇，用 sysfs 模式兜底
            _mm = {"0": "off", "1": "manual", "2": "auto"}
            fan_info[_fk] = {"name": f"风扇{_fi}", "mode": _mm.get(_pe, ""),
                             "hwmon": _hw, "idx": _fi, "controllable": _controllable}
        # PWM 占空比（0-255 → 百分比），不管装没装外部风扇服务都读
        if _pv and _fk in fan_info:
            try:
                fan_info[_fk]["pwm"] = round(int(_pv) / 255 * 100)
            except ValueError:
                pass
    if sens_j:
        try:
            j = json.loads(sens_j)
            for chip, entries in j.items():
                cs = chip.split("-")[0]
                for ename, fields in entries.items():
                    if ename == "Adapter":
                        continue
                    for fn, fv in fields.items():
                        if not fn.endswith("_input") or not isinstance(fv, (int, float)):
                            continue
                        prefix = fn.replace("_input", "")
                        if fn.startswith("temp"):
                            if fv < -50 or fv > 150:
                                break
                            mx = fields.get(f"{prefix}_max")
                            cr = fields.get(f"{prefix}_crit")
                            if cs != "coretemp":
                                mx = None
                                cr = None
                            if mx is not None and (mx < 0 or mx > 150): mx = None
                            if cr is not None and (cr < 0 or cr > 150): cr = None
                            if cs == "coretemp":
                                nm = ename
                                if "Package" in ename:
                                    cpu_temp = round(fv, 1)
                            elif cs == "acpitz":
                                nm = "主板(ACPI)"
                            elif cs.startswith("pch"):
                                nm = "PCH 芯片组"
                            elif cs.startswith("it"):
                                nm = "主板(CPU附近)" if "temp1" in ename else "主板(系统)" if "temp2" in ename else "主板"
                            else:
                                nm = ename
                            d["sensors"]["temps"].append({"name": nm, "value": round(fv, 1), "max": mx, "crit": cr})
                            break
                        elif fn.startswith("fan"):
                            fan_key = fn.replace("_input", "")
                            default_name = fan_key.replace("fan", "风扇")
                            fi = fan_info.get(fan_key, {})
                            _lab = _fan_label_for(fi.get("hwmon", ""), fi.get("idx", 0))
                            display_name = _lab.get("name") or fi.get("name", default_name)
                            mode = fi.get("mode", "")
                            pwm = fi.get("pwm")
                            d["sensors"]["fans"].append({
                                "name": display_name,
                                "label": _lab.get("name", ""),
                                "voltage": _lab.get("voltage", ""),
                                "rpm": int(fv),
                                "stopped": fv < 1,
                                "mode": mode,
                                "pwm": pwm,
                                "controllable": fi.get("controllable", False),
                                "hwmon": fi.get("hwmon", ""),
                                "idx": fi.get("idx", 0),
                                # has_tach=False：读不到转速（分线器副扇/未接转速线/主板未布线该通道）
                                "has_tach": int(fv) > 0,
                                "hidden": bool(_lab.get("hidden")),
                            })
                            break
                        elif fn.startswith("in"):
                            v = fv / 1000 if fv > 100 else fv
                            nm = ename
                            if "3.3V" in ename:
                                nm = "+3.3V"
                            elif "VSB" in ename:
                                nm = "3VSB 待机"
                            elif "bat" in ename.lower():
                                nm = "CMOS 电池"
                            d["sensors"]["voltages"].append({"name": nm, "value": round(v, 2)})
                            break
        except (json.JSONDecodeError, ValueError):
            pass
    d["cpu_temp"] = cpu_temp
    # 兼容旧字段
    d["temps"] = {t["name"]: t["value"] for t in d["sensors"]["temps"]}
    # 显卡
    lspci = run_cmd(["lspci"], 5)
    gpus = []
    for line in lspci.splitlines():
        if re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I):
            m = re.search(r"controller:\s*(.+)", line, re.I)
            gpus.append(m.group(1).strip() if m else line.strip())
    d["gpus"] = gpus
    # 网卡（只显示物理网卡和 bond，过滤 docker/虚拟网桥）
    link_out = run_cmd(["ip", "-o", "link", "show"], 5)
    addr_out = run_cmd(["ip", "-o", "addr", "show"], 5)
    nics = []
    for line in link_out.splitlines():
        m = re.match(r"\d+:\s+(\S+?):\s+<([^>]*)>.*?state\s+(\w+).*?link/(\S+)\s+(\S+)", line)
        if not m:
            continue
        name = m.group(1)
        if name == "lo" or name.startswith(("docker", "br-", "veth")):
            continue
        state = m.group(3)
        mac = m.group(5)
        speed = read_file(f"/sys/class/net/{name}/speed").strip()
        if not speed.isdigit():
            speed = ""
        ip = ""
        for aline in addr_out.splitlines():
            if re.search(rf"\b{re.escape(name)}\b", aline):
                im = re.search(r"\binet\s+(\S+)", aline)
                if im and not ip:
                    ip = im.group(1).split("/")[0]
        nics.append({"name": name, "state": state, "mac": mac, "speed": speed, "ip": ip})
    # 附加实时网速（来自采集 daemon 的 _metrics_cur，每 2s 刷新一次）
    try:
        with _METRICS_LOCK:
            _net_rt = {n["name"]: n for n in _metrics_cur.get("net", [])}
        for nic in nics:
            rt = _net_rt.get(nic["name"])
            if rt:
                nic["rx_rate"] = rt.get("rx_rate", 0.0)
                nic["tx_rate"] = rt.get("tx_rate", 0.0)
    except Exception:
        pass
    d["nics"] = nics
    # 主板 / 内存品牌型号（dmidecode），失败不影响其它采集
    try:
        d["board"] = get_board()
    except Exception:
        d["board"] = {"manufacturer": "", "product": "", "version": ""}
    try:
        d["memory_modules"] = get_memory_modules()
    except Exception:
        d["memory_modules"] = {"modules": [], "total_gb": 0, "slots": 0, "brand_summary": ""}
    return d

def format_uptime(s):
    d = int(s // 86400); h = int((s % 86400) // 3600); m = int((s % 3600) // 60)
    if d > 0:
        return f"{d}天{h}小时{m}分"
    return f"{h}小时{m}分"

def fmt_kb(kb):
    if kb >= 1048576:
        return f"{kb/1048576:.1f} GB"
    if kb >= 1024:
        return f"{kb/1024:.0f} MB"
    return f"{kb} KB"

# ===================== 采集：存储卷 =====================
def get_storage():
    d = {"raid_arrays": [], "volumes": [], "topology": ""}
    # mdadm RAID
    mdstat = read_file("/proc/mdstat")
    d["mdstat"] = mdstat
    d["topology"] = sudo_cmd(["lsblk", "-o", "NAME,SIZE,TYPE,ROTA,MODEL"], 5) or run_cmd(["lsblk"], 5)
    cur = None
    for line in mdstat.splitlines():
        # 成员盘在首行：md2 : active raid0 sda1[0] sdb1[1]
        m = re.match(r"^(md\d+)\s*:\s*(\w+)\s+(raid\d+|linear|multipath)(.*)", line)
        if m:
            if cur:
                d["raid_arrays"].append(cur)
            cur = {"name": m.group(1), "state": m.group(2), "level": m.group(3), "disks": [], "size": ""}
            for dm in re.findall(r"(sd\w+|nvme\w+)", m.group(4)):
                cur["disks"].append(dm)
        elif cur and re.match(r"^\s+\d+\s+blocks", line):
            ms = re.search(r"(\d+)\s+blocks", line)
            if ms:
                cur["size"] = fmt_blocks(int(ms.group(1)))
    if cur:
        d["raid_arrays"].append(cur)
    # 挂载点容量（排除 docker overlay / tmpfs 等非存储卷）
    df = run_cmd(["df", "-h", "--output=target,size,used,avail,pcent,fstype"], 5)
    skip_fs = ("overlay", "tmpfs", "devtmpfs", "squashfs")
    for line in df.strip().splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 6:
            mount, size, used, avail, pcent, fstype = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            if fstype in skip_fs:
                continue
            if "docker" in mount or "overlay" in mount:
                continue
            if mount in ("/", "/fs", "/boot", "/boot/efi") or mount.startswith("/vol"):
                d["volumes"].append({
                    "mount": mount, "size": size, "used": used,
                    "avail": avail, "pcent": pcent, "fstype": fstype,
                })
    return d

def fmt_blocks(blocks):
    # blocks 是 1K 块
    kb = blocks
    if kb >= 1073741824:
        return f"{kb/1073741824:.1f} TB"
    if kb >= 1048576:
        return f"{kb/1048576:.1f} GB"
    return f"{kb/1024:.0f} MB"

# ===================== 采集：Docker =====================
def _listening_ports_in_netns(pid):
    """读取某 PID 网络命名空间内处于 LISTEN 的 TCP 端口（容器内视角）"""
    ports = set()
    for f in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh, None)
                for line in fh:
                    fld = line.split()
                    if len(fld) >= 4 and fld[3] == "0A":  # 0A = LISTEN
                        ports.add(int(fld[1].split(":")[1], 16))
        except Exception:
            continue
    return sorted(ports)

def _listening_ports_for_pids(pids):
    """汇总一组进程拥有的、处于 LISTEN 的 TCP 端口（host 网络模式按进程归属判定）"""
    inodes = set()
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(f"/proc/{pid}/fd/{fd}")
                except Exception:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
        except Exception:
            continue
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh, None)
                for line in fh:
                    fld = line.split()
                    if len(fld) < 10:
                        continue
                    if fld[3] == "0A" and fld[9] in inodes:
                        ports.add(int(fld[1].split(":")[1], 16))
        except Exception:
            continue
    return sorted(ports)

def _container_pids(name):
    """用 docker top 取容器内所有进程 PID（host 网络模式端口归属用）"""
    pids = []
    out = sudo_cmd(["docker", "top", name], 5)
    for line in out.splitlines()[1:]:  # 跳过表头
        f = line.split()
        if len(f) >= 2 and f[1].isdigit():
            pids.append(int(f[1]))
    return pids

def _detect_ports(meta):
    """根据 docker inspect 信息自动探测端口号（兼容 bridge 发布端口 / host 模式真实监听端口）"""
    netmode = (meta.get("netmode") or "bridge")
    ports_map = meta.get("ports") or {}
    pid = meta.get("pid") or 0
    parts = []
    # 1) 已发布端口映射（bridge / 自定义网络，-p 映射），去重（IPv4/IPv6 双绑定）
    if ports_map:
        seen = set()
        for cport, bindings in ports_map.items():
            if bindings:
                for b in bindings:
                    hip = (b.get("HostIp") or "").strip()
                    hport = b.get("HostPort", "")
                    if hip and hip not in ("0.0.0.0", "::", "::/0"):
                        s = f"{hip}:{hport}→{cport}"
                    else:
                        s = f"{hport}→{cport}"
                    if s not in seen:
                        seen.add(s)
                        parts.append(s)
            else:
                s = f"{cport} (未发布)"
                if s not in seen:
                    seen.add(s)
                    parts.append(s)
    # 2) host 网络模式：端口即主机端口，按进程归属探测真实监听端口
    if netmode.startswith("host"):
        pids = _container_pids(meta.get("name", ""))
        if pids:
            for p in _listening_ports_for_pids(pids):
                parts.append(f"{p}/tcp")
        else:
            parts.append("host 网络")
    # 3) 非 host 且无发布端口：探测容器内部监听端口（提示性）
    if not ports_map and not netmode.startswith("host") and pid:
        for p in _listening_ports_in_netns(pid):
            parts.append(f"{p}/tcp (容器内部)")
    if not parts:
        return "-"
    return "  ".join(parts)

def _cn_status(status):
    """把 docker 的英文状态串转换为中文（含运行时长）"""
    s = (status or "").strip()
    low = s.lower()
    if low.startswith("up"):
        # 运行中：Up 3 days / Up 5 hours / Up 30 seconds / Up About a minute
        body = s[2:].strip()
        body = re.split(r"[\(（]", body)[0].strip()  # 去掉 (health: ...) 等括号
        repl = [("about a minute", "约1分钟"), ("about an hour", "约1小时"),
                ("days", "天"), ("day", "天"), ("hours", "小时"), ("hour", "小时"),
                ("minutes", "分钟"), ("minute", "分钟"), ("seconds", "秒"), ("second", "秒")]
        cn = body
        for a, b in repl:
            cn = re.sub(r"\b" + re.escape(a) + r"\b", b, cn, flags=re.I)
        cn = re.sub(r"\ba\b", "1", cn)  # 兜底 a day / a hour
        return "已运行 " + cn
    m = re.search(r"exited.*?(\d+)\s*(day|days|hour|hours|minute|minutes|second|seconds)", low)
    if m:
        num = m.group(1)
        unit = m.group(2)
        ucn = {"day": "天", "days": "天", "hour": "小时", "hours": "小时",
               "minute": "分钟", "minutes": "分钟", "second": "秒", "seconds": "秒"}[unit]
        return "已停止 (停于 %s%s前)" % (num, ucn)
    if low.startswith("created"):
        return "已创建未启动"
    return s  # 兜底原串

def _parse_docker_pct(s):
    """'12.34%' -> 12.34 ; 其它(None/空/非数字) -> None"""
    if not s or not isinstance(s, str):
        return None
    m = re.search(r"([\d.]+)", s)
    return float(m.group(1)) if m else None

def _docker_size_to_bytes(s):
    """'120MiB' / '1.2kB' -> int 字节数 ; 无效 -> None"""
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgGtT]?)i?B\s*$", s.strip())
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "").upper()
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(num * mult.get(unit, 1))

def _split_netio(s):
    """'1.2kB / 3.4kB' -> (rx_bytes, tx_bytes)；无效 -> (None, None)"""
    if not s or not isinstance(s, str):
        return (None, None)
    parts = [p.strip() for p in s.split("/") if p.strip()]
    rx = _docker_size_to_bytes(parts[0]) if len(parts) >= 1 else None
    tx = _docker_size_to_bytes(parts[1]) if len(parts) >= 2 else None
    return (rx, tx)

def get_docker():
    """统计 Docker 容器数（运行中/总数），并自动探测每个容器真实监听端口与资源占用"""
    try:
        out = sudo_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"], 8)
        containers = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            name = parts[0].strip() if len(parts) > 0 else ""
            status = parts[1].strip() if len(parts) > 1 else ""
            image = parts[2].strip() if len(parts) > 2 else ""
            running = status.lower().startswith("up") or "running" in status.lower()
            containers.append({"name": name, "status": status, "image": image, "ports": "-", "running": running,
                               "mem": None, "cpu": None, "mem_pct": None, "mem_bytes": None,
                               "net_rx": None, "net_tx": None, "runtime": _cn_status(status)})
        # 批量 inspect 取端口 / pid / 网络模式，自动探测端口
        try:
            ids = sudo_cmd(["docker", "ps", "-a", "-q"], 8).split()
            if ids:
                raw = sudo_cmd(["docker", "inspect"] + ids, 15)
                data = json.loads(raw) if raw.strip() else []
                info = {}
                for c in data:
                    nm = (c.get("Name") or "").lstrip("/")
                    info[nm] = {
                        "netmode": (c.get("HostConfig", {}).get("NetworkMode") or "bridge"),
                        "pid": c.get("State", {}).get("Pid", 0),
                        "ports": c.get("NetworkSettings", {}).get("Ports") or {},
                        "name": nm,
                    }
                for c in containers:
                    meta = info.get(c["name"])
                    if meta:
                        c["ports"] = _detect_ports(meta)
        except Exception:
            # 兜底：用 docker ps 的 Ports 字段
            try:
                out2 = sudo_cmd(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Ports}}"], 8)
                pm = {}
                for line in out2.splitlines():
                    line = line.strip()
                    if not line or "|" not in line:
                        continue
                    n, p = line.split("|", 1)
                    pm[n.strip()] = p.strip()
                for c in containers:
                    if not c["ports"] or c["ports"] == "-":
                        c["ports"] = pm.get(c["name"], "-")
            except Exception:
                pass
        # 运行中容器的资源占用（docker stats 仅对运行中容器有数据）：CPU% / 内存% / 内存字节 / 网络 RX-TX
        try:
            stat = sudo_cmd(["docker", "stats", "--no-stream", "--format",
                             "{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}|{{.NetIO}}"], 12)
            for line in stat.splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                cname, cpu_s, memp_s, mem_s, net_s = parts[0], parts[1], parts[2], parts[3], parts[4]
                cname = cname.strip()
                rx, tx = _split_netio(net_s)
                for c in containers:
                    if c["name"] == cname:
                        c["cpu"] = _parse_docker_pct(cpu_s)
                        c["mem_pct"] = _parse_docker_pct(memp_s)
                        c["mem"] = mem_s.strip()
                        c["mem_bytes"] = _docker_size_to_bytes(mem_s.split("/")[0].strip()) if "/" in mem_s else _docker_size_to_bytes(mem_s.strip())
                        c["net_rx"] = rx
                        c["net_tx"] = tx
                        break
        except Exception:
            pass
        # 停止的容器且无端口配置 → 标注「容器停止不检测端口」，避免与「运行中但无端口」的 "-" 混淆
        for c in containers:
            if c["ports"] in ("-", "") and not c["running"]:
                c["ports"] = "容器停止不检测端口"
        running = sum(1 for c in containers if c["running"])
        return {"running": running, "total": len(containers), "containers": containers, "ok": True}
    except Exception:
        return {"running": 0, "total": 0, "containers": [], "ok": False}

# ===================== 路由 =====================
@app.route("/")
def index():
    # no-store：防止浏览器/代理缓存 HTML，避免发版或重启后用户仍看到旧页面（曾导致 FCS 卡片永久“加载中”）
    resp = make_response(render_template("index.html", APP_VERSION=APP_VERSION))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ===================== 采集层：实时指标（网络吞吐 / 磁盘 I/O / CPU 功耗） =====================
# 这些指标需「两次采样差」才算速率，故由常驻 daemon 线程周期采样，/api/all 仅读最新值。
# 模式复用 fan_smooth_loop 的 daemon 线程做法。
_METRICS_LOCK = _threading.Lock()
_metrics_prev = {"net": {}, "disk": {}, "rapl": None, "rapl_t": 0.0}
_metrics_cur = {"net": [], "disk": [], "cpu_power_w": 0.0, "cpu_power_valid": False}
_CPU_POWER_EMA = None

def _read_net_bytes():
    """返回 {iface: (rx_bytes, tx_bytes)}，过滤回环/虚拟接口"""
    res = {}
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo" or name.startswith(("docker", "br-", "veth")):
                continue
            parts = data.split()
            if len(parts) < 9:
                continue
            res[name] = (int(parts[0]), int(parts[8]))
    except Exception:
        pass
    return res

def _read_disk_sectors():
    """返回 {dev: (rd_sectors, wr_sectors)}，过滤分区(数字结尾)与 loop/ram"""
    res = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                cols = line.split()
                if len(cols) < 11:
                    continue
                dev = cols[2]
                if dev.startswith(("loop", "ram")) or dev[-1].isdigit():
                    continue
                res[dev] = (int(cols[5]), int(cols[9]))
    except Exception:
        pass
    return res

def _read_rapl_energy():
    """读 CPU 封装能耗(微焦)，root 可读；返回 energy_uj 或 None。admin 无权限→None。"""
    base = "/sys/class/powercap/intel-rapl/intel-rapl:0"
    try:
        return int(open(base + "/energy_uj").read().strip())
    except Exception:
        return None

# ===================== 历史趋势：SQLite 存储（免维护，30天自清理） =====================
import sqlite3 as _sqlite3
_DB_PATH = os.path.join(_config_dir(), "history.db")
_db_lock = _threading.Lock()
_db_last_write = 0.0

def _init_history_db():
    try:
        with _db_lock:
            con = _sqlite3.connect(_DB_PATH)
            con.execute("""CREATE TABLE IF NOT EXISTS samples(
                ts INTEGER PRIMARY KEY,
                disk_read REAL, disk_write REAL,
                net_rx REAL, net_tx REAL, cpu_power REAL)""")
            con.commit(); con.close()
    except Exception:
        pass

def _write_history_sample():
    """把当前实时指标聚合一行写入 SQLite；并删除 30 天前样本（自清理）。"""
    global _db_last_write
    try:
        now = int(time.time())
        with _METRICS_LOCK:
            disk = _metrics_cur["disk"]; net = _metrics_cur["net"]
            cpu = _metrics_cur.get("cpu_power_w", 0.0)
        dr = sum((d.get("read_rate") or 0) for d in disk)
        dw = sum((d.get("write_rate") or 0) for d in disk)
        nr = sum((n.get("rx_rate") or 0) for n in net)
        nw = sum((n.get("tx_rate") or 0) for n in net)
        with _db_lock:
            con = _sqlite3.connect(_DB_PATH)
            con.execute(
                "INSERT OR REPLACE INTO samples(ts,disk_read,disk_write,net_rx,net_tx,cpu_power) VALUES(?,?,?,?,?,?)",
                (now, dr, dw, nr, nw, cpu))
            cutoff = now - 30*86400
            con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            con.commit(); con.close()
        _db_last_write = time.time()
    except Exception:
        pass

_init_history_db()

def metrics_collect_loop():
    """daemon 线程：每 ~2s 采样一次，计算速率/功率并写入 _metrics_cur"""
    global _CPU_POWER_EMA
    while True:
        try:
            time.sleep(2)
            now = time.time()
            # 历史趋势：每 30s 聚合写一行 SQLite（免维护，超 30 天自动清理）
            if now - _db_last_write >= 30:
                _write_history_sample()
            # 网络吞吐
            net_now = _read_net_bytes()
            net_out = []
            with _METRICS_LOCK:
                prev = _metrics_prev["net"]
                for iface, (rx, tx) in net_now.items():
                    prx, ptx = prev.get(iface, (rx, tx))
                    dt = 2.0
                    rx_rate = max(0.0, (rx - prx) / dt)
                    tx_rate = max(0.0, (tx - ptx) / dt)
                    net_out.append({
                        "name": iface,
                        "rx_rate": round(rx_rate, 1),   # bytes/s，前端动态格式化为 B/s/KB/s/MB/s
                        "tx_rate": round(tx_rate, 1),
                        "rx_total_mb": round(rx / 1048576, 1),
                        "tx_total_mb": round(tx / 1048576, 1),
                    })
                _metrics_prev["net"] = net_now
                _metrics_cur["net"] = net_out
            # 磁盘 I/O
            disk_now = _read_disk_sectors()
            disk_out = []
            with _METRICS_LOCK:
                prev = _metrics_prev["disk"]
                for dev, (rd, wr) in disk_now.items():
                    prd, pwr = prev.get(dev, (rd, wr))
                    dt = 2.0
                    rd_rate = max(0.0, (rd - prd) * 512 / dt)
                    wr_rate = max(0.0, (wr - pwr) * 512 / dt)
                    disk_out.append({
                        "device": dev,
                        "read_rate": round(rd_rate, 1),   # bytes/s，前端动态格式化为 B/s/KB/s/MB/s
                        "write_rate": round(wr_rate, 1),
                    })
                _metrics_prev["disk"] = disk_now
                _metrics_cur["disk"] = disk_out
            # CPU 封装功耗 (RAPL)：两次采样差算功率 + EMA 平滑
            e = _read_rapl_energy()
            with _METRICS_LOCK:
                pe = _metrics_prev["rapl"]; pt = _metrics_prev["rapl_t"]
                if e is not None and pe is not None and pt:
                    dt = now - pt
                    if dt > 0:
                        w = (e - pe) / 1e6 / dt
                        if 0 < w < 1000:   # 合理性过滤（微焦回绕/异常）
                            _CPU_POWER_EMA = w if _CPU_POWER_EMA is None else (_CPU_POWER_EMA * 0.8 + w * 0.2)
                            _metrics_cur["cpu_power_w"] = round(_CPU_POWER_EMA, 2)
                            _metrics_cur["cpu_power_valid"] = True
                _metrics_prev["rapl"] = e
                _metrics_prev["rapl_t"] = now
        except Exception:
            time.sleep(2)

_metrics_thread = _threading.Thread(target=metrics_collect_loop, daemon=True, name="metrics")
_metrics_thread.start()

def get_realtime_metrics():
    with _METRICS_LOCK:
        return {
            "net": _metrics_cur["net"],
            "disk": _metrics_cur["disk"],
            "cpu_power_w": _metrics_cur["cpu_power_w"],
            "cpu_power_valid": _metrics_cur["cpu_power_valid"],
        }

@app.route("/api/all")
def api_all():
    t0 = time.time()
    try:
        try:
            board = get_board()
        except Exception:
            board = {"manufacturer": "", "product": "", "version": ""}
        try:
            memory_modules = get_memory_modules()
        except Exception:
            memory_modules = {"modules": [], "total_gb": 0, "slots": 0, "brand_summary": ""}
        result = {
            "raid": get_raid_card(),
            "disks": get_disks(),
            "system": {**get_system(), "board": board, "memory_modules": memory_modules},
            "storage": get_storage(),
            "docker": get_docker(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 2),
        }
        try:
            rt = get_realtime_metrics()
            result["net"] = rt["net"]
            result["diskio"] = rt["disk"]
            # 给 diskio 补上型号/容量/品牌等友好标识，方便用户识别 sda/sdb 对应哪块盘
            disk_map = {d["dev"]: d for d in result.get("disks", [])}
            for d in result["diskio"]:
                info = disk_map.get(d["device"], {})
                d["model"] = info.get("model", "")
                d["size"] = info.get("size", "")
                d["brand"] = info.get("brand", "")
                d["type"] = info.get("type", "")
                d["serial"] = info.get("serial", "")
        except Exception:
            pass
        # 活动告警（复用已采集数据，无额外命令开销）
        try:
            result["alerts"] = _evaluate_alerts(result["system"], result["disks"], result["docker"])
        except Exception:
            result["alerts"] = []
    except Exception as e:
        result = {"error": str(e), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    return jsonify(result)

# ===================== 按板块独立接口（方案B：切换导航只拉当前板块，避免全量 /api/all）=====================
def _panel_time():
    return time.strftime("%Y-%m-%d %H:%M:%S")

@app.route("/api/system")
def api_system():
    """系统资源板块（CPU/内存/温度/风扇/GPU/网卡 + 主板/内存条），供 #system 与 #fan 按需刷新。"""
    t0 = time.time()
    try:
        try:
            board = get_board()
        except Exception:
            board = {"manufacturer": "", "product": "", "version": ""}
        try:
            memory_modules = get_memory_modules()
        except Exception:
            memory_modules = {"modules": [], "total_gb": 0, "slots": 0, "brand_summary": ""}
        system = {**get_system(), "board": board, "memory_modules": memory_modules}
        return jsonify({"system": system, "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/raid")
def api_raid():
    """阵列卡板块。#raid 渲染同时依赖 raid + disks，故一并返回（两者均带 12s 缓存）。"""
    t0 = time.time()
    try:
        return jsonify({"raid": get_raid_card(), "disks": get_disks(),
                        "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/disks")
def api_disks():
    """硬盘 SMART 板块。"""
    t0 = time.time()
    try:
        return jsonify({"disks": get_disks(), "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/storage")
def api_storage():
    """存储卷板块（mdadm/lsblk/df，均为本地快速命令）。"""
    t0 = time.time()
    try:
        return jsonify({"storage": get_storage(), "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/docker")
def api_docker():
    """Docker 容器板块。"""
    t0 = time.time()
    try:
        return jsonify({"docker": get_docker(), "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/metrics")
def api_metrics():
    """轻量实时指标：网络吞吐 + 磁盘 I/O。供前端高频(2s)轮询，不触发重型 /api/all(阵列卡/SMART等)。"""
    try:
        rt = get_realtime_metrics()
        diskio = rt["disk"]
        try:
            disk_map = {d["dev"]: d for d in get_disks()}
        except Exception:
            disk_map = {}
        for d in diskio:
            info = disk_map.get(d["device"], {})
            d["model"] = info.get("model", "")
            d["size"] = info.get("size", "")
            d["brand"] = info.get("brand", "")
            d["type"] = info.get("type", "")
        return jsonify({"net": rt["net"], "diskio": diskio})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/history")
def api_history():
    """返回历史趋势降采样点（磁盘读/写速率），按 range 时间桶聚合，省流量。"""
    try:
        rng = request.args.get("range", "24h")
        secs = {"24h": 86400, "7d": 7*86400, "30d": 30*86400}.get(rng, 86400)
        end = int(time.time()); start = end - secs
        bucket = max(60, secs // 240)
        with _db_lock:
            con = _sqlite3.connect(_DB_PATH)
            rows = con.execute(
                "SELECT (ts/?)*?*1000 AS bts, AVG(disk_read), AVG(disk_write) "
                "FROM samples WHERE ts>=? GROUP BY bts ORDER BY bts",
                (bucket, bucket, start)).fetchall()
            con.close()
        points = [{"ts": r[0], "disk_read": round(r[1] or 0, 1), "disk_write": round(r[2] or 0, 1)} for r in rows]
        return jsonify({"range": rng, "points": points, "bucket_s": bucket})
    except Exception as e:
        return jsonify({"error": str(e), "points": []})

@app.route("/api/fan/set", methods=["POST"])
@require_admin()
def api_fan_set():
    """设置风扇转速：设目标 PWM（后台缓变线程平滑过渡）或恢复自动控温"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    hwmon = data.get("hwmon")
    idx = data.get("idx")
    mode = data.get("mode")
    pwm = data.get("pwm")
    # 安全：仅允许本机 hwmon 路径，防止路径注入
    if not isinstance(hwmon, str) or not hwmon.startswith("/sys/class/hwmon/hwmon"):
        return jsonify({"ok": False, "error": "invalid hwmon"}), 400
    try:
        idx = int(idx)
        if idx < 1 or idx > 10:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid idx"}), 400
    key = (hwmon, idx)
    # 防御：hwmon 路径可能跨重启/进程漂移(如 nct6797 在 hwmon3↔hwmon4 间变化)，
    # 而 FAN_TARGETS 与平滑线程都以「实时枚举」为权威 key。若 GUI 发来的 (hwmon,idx)
    # 不在实时枚举中、但该 idx 存在，则按 idx 校正 hwmon，确保目标命中真正的可控通道。
    _enum = _enumerate_fans()
    if (hwmon, idx) not in _enum:
        _idx2hw = {i: h for (h, i) in _enum}
        if idx in _idx2hw:
            hwmon = _idx2hw[idx]
            key = (hwmon, idx)
    if mode == "auto":
        if _fan_ext_service_running():
            # 系统风扇服务在跑：交还它接管（写 enable=2）
            try:
                with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
                    f.write("2")
            except Exception:
                pass
            with FAN_LOCK:
                FAN_TARGETS.pop(key, None)
            _save_fan_mode(idx, "auto", None)
            return jsonify({"ok": True, "mode": "auto", "owner": "ext_service"})
        # 系统风扇服务未运行：nasdash 自带保守温控曲线接管
        with FAN_LOCK:
            FAN_TARGETS[key] = {"mode": "auto", "target": None}
        _save_fan_mode(idx, "auto", None)
        return jsonify({"ok": True, "mode": "auto", "owner": "nasdash"})
    try:
        pct = int(pwm)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid pwm"}), 400
    FLOOR = 10  # 最低 10%（用户要求支持 10% 档位；此硬件风扇 10% 仍可运转）
    pct = max(FLOOR, min(100, pct))
    raw = round(pct / 100 * 255)
    # 仅设为目标值，由缓变线程平滑过渡（不再瞬间写 255，避免突然全速）
    with FAN_LOCK:
        FAN_TARGETS[key] = {"mode": "manual", "target": raw}
    _save_fan_mode(idx, "manual", raw)
    return jsonify({"ok": True, "mode": "manual", "pwm": pct, "raw": raw})


def get_fan_status():
    """风扇实时状态列表（供前端轮询与硬件健康报告复用）。"""
    fans = []
    labels = _load_fan_labels()
    _dt = _load_fan_disk_temp()
    _dt_active = bool(_dt.get("enabled")) and bool(_dt.get("disks"))
    _dt_cf = _dt.get("controlled_fans", "all")
    _st = _load_fan_sys_temp()
    _st_active = bool(_st.get("enabled"))
    _st_cf = _st.get("controlled_fans", "all")
    fc_raw = read_file("/vol2/@appconf/FanControlServer/config.json")
    names = {}
    if fc_raw:
        try:
            fc = json.loads(fc_raw)
            for fan in fc.get("fans", []):
                fi = fan.get("pwm_index")
                if fi:
                    names[fi] = fan.get("name", f"风扇{fi}")
        except Exception:
            pass
    # 复用 _enumerate_fans：不依赖芯片型号、遍历所有 hwmon 风扇通道（fan1-10）、多芯片不漏
    for (hwmon, idx) in _enumerate_fans():
        _pe = read_file(f"{hwmon}/pwm{idx}_enable").strip()
        _pv = read_file(f"{hwmon}/pwm{idx}").strip()
        _fv = read_file(f"{hwmon}/fan{idx}_input").strip()
        try:
            rpm = int(_fv)
        except Exception:
            rpm = 0
        try:
            pwm_raw = int(_pv)
        except Exception:
            pwm_raw = None
        pwm_pct = round(pwm_raw / 255 * 100) if pwm_raw is not None else None
        cur_mode = "manual" if _pe == "1" else "auto" if _pe == "2" else "off"
        _is_st = bool(_st_active) and (_st_cf == "all" or [hwmon, idx] in _st_cf)
        _is_dt = (not _is_st) and bool(_dt_active) and (_dt_cf == "all" or [hwmon, idx] in _dt_cf)
        mode = "sys_temp" if _is_st else ("disk_temp" if _is_dt else cur_mode)
        key = (hwmon, idx)
        target_pct = None
        with FAN_LOCK:
            tcfg = FAN_TARGETS.get(key)
        if tcfg and tcfg.get("mode") == "manual":
            target_pct = round(tcfg["target"] / 255 * 100)
        _lbl = labels.get(f"{hwmon}::{idx}", {})
        fans.append({
            "name": _lbl.get("name") or names.get(idx, f"风扇{idx}"),
            "label": _lbl.get("name", ""),
            "voltage": _lbl.get("voltage", ""),
            "idx": idx, "hwmon": hwmon,
            "rpm": rpm, "pwm": pwm_pct,
            "mode": mode,
            "target_pct": target_pct,
            "controllable": True,
            # has_tach=False：该通道读不到转速（分线器副扇/未接转速线/主板未布线该通道）
            "has_tach": rpm > 0,
            # 用户可把「无风扇的幽灵通道」隐藏（持久化到 fan_labels.json）
            "hidden": bool(_lbl.get("hidden")),
        })
    return fans

@app.route("/api/fan/status")
def api_fan_status():
    """轻量风扇状态：供前端高频轮询，实时显示转速/当前占空比/目标（常驻线程 2s tick）"""
    return jsonify({"fans": get_fan_status()})


@app.route("/api/fan/disk_temp")
def api_fan_disk_temp_get():
    """读取硬盘温度控风扇配置 + 实时监控盘温度/休眠 + 计算所得目标PWM"""
    cfg = _load_fan_disk_temp()
    devs = cfg.get("disks", [])
    states = get_disk_temps(devs) if devs else {}
    disks_out = [{
        "dev": dev,
        "temp": states.get(dev, {}).get("temp"),
        "asleep": states.get(dev, {}).get("asleep"),
    } for dev in devs]
    target = _fan_disk_temp_pwm(states, cfg)
    return jsonify({
        "config": cfg,
        "disks": disks_out,
        "computed_pwm": round(target / 255 * 100) if target is not None else None,
        "computed_raw": target,
    })


@app.route("/api/fan/disk_temp", methods=["POST"])
@require_admin()
def api_fan_disk_temp_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_fan_disk_temp()
    if "disks" in data:
        if not isinstance(data["disks"], list):
            return jsonify({"ok": False, "error": "disks 需为数组"}), 400
        norm = []
        for d in data["disks"]:
            # 前端勾选值可能是短名（sda / nvme0n1），也可能是已带 /dev/ 的全路径
            devname = str(d["dev"] if isinstance(d, dict) and "dev" in d else d)
            dd = devname if devname.startswith("/dev/") else "/dev/" + devname
            if not os.path.exists(dd):
                return jsonify({"ok": False, "error": "设备不存在: " + dd}), 400
            norm.append(dd)
        cfg["disks"] = norm
    if "sleep_stop" in data:
        cfg["sleep_stop"] = bool(data["sleep_stop"])
    err = _apply_temp_curve(cfg, data, recover_max=100)
    if err:
        return err
    if _save_fan_disk_temp(cfg):
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": False, "error": "写配置失败"}), 500


@app.route("/api/fan/sys_temp")
def api_fan_sys_temp_get():
    """读取主板/CPU 温度控风扇配置 + 当前温度源读数 + 计算所得目标PWM"""
    cfg = _load_fan_sys_temp()
    T = _fan_read_sys_temp(cfg.get("source", "cpu"))
    target = _fan_sys_temp_pwm(T, cfg) if T is not None else None
    return jsonify({
        "config": cfg,
        "source_temp": round(T, 1) if T is not None else None,
        "computed_pwm": round(target / 255 * 100) if target is not None else None,
        "computed_raw": target,
    })


@app.route("/api/fan/sys_temp", methods=["POST"])
@require_admin()
def api_fan_sys_temp_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_fan_sys_temp()
    if "source" in data:
        if data["source"] not in ("cpu", "mb"):
            return jsonify({"ok": False, "error": "source 需为 cpu 或 mb"}), 400
        cfg["source"] = data["source"]
    err = _apply_temp_curve(cfg, data, recover_max=120)
    if err:
        return err
    if _save_fan_sys_temp(cfg):
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": False, "error": "写配置失败"}), 500




@app.route("/api/fan/fcs")
def api_fan_fcs_get():
    """FanControlServer 状态：是否安装 / 开机自启 / 运行中 / 被用户永久禁用。"""
    return jsonify(_fcs_status())


@app.route("/api/fan/fcs", methods=["POST"])
@require_admin()
def api_fan_fcs_post():
    """永久禁用 / 恢复 FanControlServer。body: {"action": "disable"|"enable"}。
    disable = systemctl stop + disable（重启不复活）+ 持久化标志；enable = enable + start。"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    action = data.get("action")
    if action == "disable":
        _fcs_disable()
    elif action == "enable":
        _fcs_enable()
    else:
        return jsonify({"ok": False, "error": "action 需为 disable 或 enable"}), 400
    return jsonify({"ok": True, "status": _fcs_status()})


@app.route("/api/fan/labels", methods=["GET"])
def api_fan_labels_get():
    """返回用户标注的风扇名称/电压：key="hwmon::idx" -> {"name","voltage"}"""
    return jsonify(_load_fan_labels())


@app.route("/api/fan/labels", methods=["POST"])
@require_admin()
def api_fan_labels_post():
    """保存风扇标注（整体覆盖）。body: {"hwmon::idx": {"name":"...","voltage":"12V"}, ...}"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    clean = {}
    for k, v in data.items():
        if not isinstance(k, str) or "::" not in k:
            continue
        hwmon, idx = k.split("::", 1)
        # 安全：仅允许本机 hwmon 路径 + 合法 idx，防止注入
        if not hwmon.startswith("/sys/class/hwmon/hwmon"):
            continue
        try:
            int(idx)
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        name = str(v.get("name", ""))[:40]
        volt = v.get("voltage", "")
        if volt not in _FAN_VOLT_ALLOWED:
            volt = "未知"
        entry = {"name": name, "voltage": volt}
        if v.get("hidden"):
            entry["hidden"] = True   # 隐藏无风扇的幽灵通道（可恢复）
        clean[k] = entry
    if _save_fan_labels(clean):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "save failed"}), 500


@app.route("/api/version")
def api_version():
    """检测新版本：返回 {current, latest, update_available, url, error}。force=1 强制刷新缓存。"""
    if request.args.get("force") == "1":
        _VERSION_CHECK["checked_at"] = 0  # 使缓存失效，触发重查
    return jsonify(_check_latest_version())

@app.route("/api/board/set", methods=["POST"])
@require_admin()
def api_board_set():
    """保存/清除主板型号手动标注（白牌板 DMI 为空时由用户填写）"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    model = (data.get("model") or "").strip()
    if model:
        # 安全：限制长度、过滤换行与路径字符
        if len(model) > 60 or re.search(r"[\r\n/\\]", model):
            return jsonify({"ok": False, "error": "invalid model"}), 400
        try:
            with open(BOARD_OVERRIDE_FILE, "w", encoding="utf-8") as f:
                f.write(model)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    else:
        # 清空标注
        try:
            if os.path.exists(BOARD_OVERRIDE_FILE):
                os.remove(BOARD_OVERRIDE_FILE)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "model": model or ""})


# ===================== 控制与自动化：告警 + 健康报告 =====================
ALERTS_FILE = os.path.join(_config_dir(), "alerts.json")
_NOTIFY_LOG = os.path.join(_config_dir(), "notifications.log")

def _load_alerts():
    d = _load_json_file(ALERTS_FILE, {})
    d.setdefault("enabled", True)
    d.setdefault("temp", {"enabled": True, "cpu_max": 85, "mb_max": 75, "disk_max": 60})
    d.setdefault("disk_health", True)
    d.setdefault("memory_max", 90)
    d.setdefault("channels", {
        "system": True,
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "bark": {"enabled": False, "url": ""},
        "email": {"enabled": False, "smtp_host": "", "smtp_port": 465, "user": "", "pass": "", "to": ""},
    })
    return d

def _save_alerts(cfg):
    return _save_json_file(ALERTS_FILE, cfg)

def _mb_temp_from_sensors(sensors):
    temps = (sensors or {}).get("temps", []) or []
    cand = []
    for t in temps:
        name = (t.get("name") or "")
        if any(k in name for k in ("主板", "PCH", "芯片组", "南桥", "PCIe", "System")):
            try:
                cand.append(float(t.get("value")))
            except (TypeError, ValueError):
                pass
    return max(cand) if cand else None

def _evaluate_alerts(system, disks, docker=None):
    """扫描当前状态，返回活动告警列表（每项 {level, title, detail}）。纯内存计算，无命令执行。"""
    alerts = []
    if not system or not isinstance(system, dict):
        return alerts
    cfg = _load_alerts()
    if not cfg.get("enabled"):
        return alerts
    tcfg = cfg.get("temp", {}) or {}
    if tcfg.get("enabled") and tcfg.get("cpu_max"):
        ct = system.get("cpu_temp")
        try:
            if ct is not None and float(ct) >= float(tcfg["cpu_max"]):
                alerts.append({"level": "danger", "title": "CPU 温度过高", "detail": "CPU 封装温度 %s°C，超过阈值 %s°C" % (ct, tcfg["cpu_max"])})
        except (TypeError, ValueError):
            pass
    if tcfg.get("enabled") and tcfg.get("mb_max"):
        mt = _mb_temp_from_sensors(system.get("sensors"))
        try:
            if mt is not None and mt >= float(tcfg["mb_max"]):
                alerts.append({"level": "danger", "title": "主板/芯片组温度过高", "detail": "温度 %s°C，超过阈值 %s°C" % (mt, tcfg["mb_max"])})
        except (TypeError, ValueError):
            pass
    if tcfg.get("enabled") and tcfg.get("disk_max"):
        for d in (disks or []):
            if not isinstance(d, dict):
                continue
            dt = d.get("temp")
            try:
                if dt is not None and float(dt) >= float(tcfg["disk_max"]):
                    alerts.append({"level": "warn", "title": "硬盘温度过高", "detail": "%s 温度 %s°C，超过阈值 %s°C" % (d.get("dev", "?"), dt, tcfg["disk_max"])})
            except (TypeError, ValueError):
                pass
    if cfg.get("disk_health"):
        for d in (disks or []):
            if not isinstance(d, dict):
                continue
            h = d.get("health")
            if d.get("health_ok") is False and h not in (None, "", "N/A", "UNKNOWN"):
                alerts.append({"level": "danger", "title": "硬盘健康异常", "detail": "%s SMART 健康状态：%s" % (d.get("dev", "?"), h)})
    mm = cfg.get("memory_max")
    mem = system.get("memory") or {}
    try:
        if mm and mem.get("percent") is not None and float(mem["percent"]) >= float(mm):
            alerts.append({"level": "warn", "title": "内存占用过高", "detail": "内存使用率 %s%%，超过阈值 %s%%" % (mem["percent"], mm)})
    except (TypeError, ValueError):
        pass
    return alerts

def _notify_log(msg):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_NOTIFY_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (ts, msg))
    except Exception:
        pass

def _send_telegram(token, chat_id, text):
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        req = urllib.request.Request(url, data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        return str(e)

def _send_bark(url, text):
    try:
        if not url.endswith("/"):
            url += "/"
        req = urllib.request.Request(url + "push", data=json.dumps({"title": "nasdash 告警", "body": text}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status in (200, 201)
    except Exception as e:
        return str(e)

def _send_email(cfg, text):
    try:
        import smtplib
        from email.mime.text import MimeText
        msg = MimeText("nasdash 告警通知\n\n%s\n\n时间：%s" % (text, time.strftime("%Y-%m-%d %H:%M:%S")), "plain", "utf-8")
        msg["Subject"] = "nasdash 告警"
        msg["From"] = cfg.get("user", "")
        msg["To"] = cfg.get("to", "")
        with smtplib.SMTP_SSL(cfg.get("smtp_host"), int(cfg.get("smtp_port", 465)), timeout=10) as s:
            s.login(cfg.get("user"), cfg.get("pass"))
            s.sendmail(cfg.get("user"), [cfg.get("to")], msg.as_string())
        return True
    except Exception as e:
        return str(e)

def _dispatch_notifications(text, cfg):
    """按配置把告警文本推送到各启用渠道，返回 {channel: ok|error}。"""
    res = {}
    ch = cfg.get("channels", {}) or {}
    if ch.get("telegram", {}).get("enabled"):
        t = ch["telegram"]
        res["telegram"] = _send_telegram(t.get("bot_token", ""), t.get("chat_id", ""), text)
    if ch.get("bark", {}).get("enabled"):
        res["bark"] = _send_bark(ch["bark"].get("url", ""), text)
    if ch.get("email", {}).get("enabled"):
        res["email"] = _send_email(ch["email"], text)
    if ch.get("system", True):
        _notify_log("通知：" + text)
        res["system"] = True
    return res

def build_health_report():
    """汇总当前全部硬件状态 + 活动告警，生成健康报告快照。"""
    try:
        board = get_board()
    except Exception:
        board = {"manufacturer": "", "product": "", "version": ""}
    try:
        memory_modules = get_memory_modules()
    except Exception:
        memory_modules = {"modules": [], "total_gb": 0, "slots": 0, "brand_summary": ""}
    raid = get_raid_card()
    disks = get_disks()
    system = get_system()
    system_full = {**system, "board": board, "memory_modules": memory_modules}
    storage = get_storage()
    docker = get_docker()
    try:
        fans = get_fan_status()
    except Exception:
        fans = []
    alerts = _evaluate_alerts(system_full, disks, docker)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": APP_VERSION,
        "host": system.get("hostname"),
        "uptime": system.get("uptime"),
        "raid": raid, "disks": disks, "system": system_full,
        "storage": storage, "docker": docker, "fans": fans, "alerts": alerts,
    }

def _render_report_html(rep):
    def esc(s):
        return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def fmt(v, suffix=""):
        if v is None or v == "":
            return "—"
        return esc(v) + suffix

    # AIDA64 风样式（全部内嵌，下载后本地打开亦正常显示、可打印）
    CSS = """
    *{box-sizing:border-box}
    body{font-family:'Segoe UI',Tahoma,Arial,'Microsoft YaHei',sans-serif;color:#1a1a1a;
         max-width:1000px;margin:0 auto;padding:24px 28px;background:#fff;line-height:1.55;font-size:13px}
    .rep-title{font-size:23px;font-weight:700;color:#1f4e79;margin:0 0 2px;letter-spacing:.3px}
    .rep-sub{color:#5a6b7b;font-size:12.5px;margin:0 0 4px}
    .rep-sub b{color:#1a1a1a;font-weight:600}
    .cat{background:#1f4e79;color:#fff;font-weight:700;font-size:14px;
         padding:7px 12px;margin:20px 0 0;border-radius:3px 3px 0 0;letter-spacing:.5px}
    .cat:first-of-type{margin-top:0}
    table.props{width:100%;border-collapse:collapse;border:1px solid #d4dce6;border-top:none;margin-bottom:4px}
    table.props td.k{width:33%;background:#eef3f8;font-weight:600;
         padding:6px 12px;border-bottom:1px solid #d4dce6;vertical-align:top;color:#243b53}
    table.props td.v{padding:6px 12px;border-bottom:1px solid #d4dce6;vertical-align:top}
    table.data{width:100%;border-collapse:collapse;border:1px solid #d4dce6;margin-bottom:4px}
    table.data th{background:#336699;color:#fff;font-weight:600;text-align:left;
         padding:6px 10px;font-size:12px;border:1px solid #336699;white-space:nowrap}
    table.data td{padding:5px 10px;border:1px solid #d4dce6;vertical-align:top}
    table.data tr:nth-child(even) td{background:#f4f8fb}
    .empty{color:#5a6b7b;padding:9px 12px;border:1px solid #d4dce6;border-top:none;font-style:italic}
    .note{background:#eef3f8;border:1px solid #d4dce6;border-top:none;padding:8px 12px;
         color:#243b53;font-size:12px;margin-bottom:4px}
    .alert-box{border:1px solid #d4dce6;border-top:none;padding:10px 14px}
    .alert-box ul{margin:0;padding-left:20px}
    .alert-box li{margin:3px 0}
    .ok{color:#1a7f37} .warn{color:#b45309} .danger{color:#c0392b}
    .footer{color:#5a6b7b;font-size:12px;margin-top:26px;border-top:1px solid #d4dce6;padding-top:10px}
    @media print{body{padding:0}
      .cat,table.data th{print-color-adjust:exact;-webkit-print-color-adjust:exact}}
    """
    def cat(title):
        return f"<div class='cat'>{esc(title)}</div>"
    def props(rows):
        if not rows:
            return "<div class='empty'>（无）</div>"
        body = "".join(
            f"<tr><td class='k'>{esc(k)}</td><td class='v'>{fmt(v)}</td></tr>"
            for k, v in rows)
        return f"<table class='props'>{body}</table>"
    def data(headers, rows):
        if not rows:
            return "<div class='empty'>（无）</div>"
        th = "".join(f"<th>{esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
            for r in rows)
        return f"<table class='data'><tr>{th}</tr>{body}</table>"
    def note(s):
        return f"<div class='note'>{esc(s)}</div>"

    alerts = rep.get("alerts", []) or []
    sys_ = rep.get("system", {}) or {}
    mem = sys_.get("memory", {}) or {}
    swap = sys_.get("swap", {}) or {}
    board = sys_.get("board", {}) or {}
    mm = sys_.get("memory_modules", {}) or {}
    sens = sys_.get("sensors", {}) or {}
    nics = sys_.get("nics", []) or []
    gpus = sys_.get("gpus", []) or []
    disks = rep.get("disks", []) or []
    raid = rep.get("raid", {}) or {}
    storage = rep.get("storage", {}) or {}
    docker = rep.get("docker", {}) or {}
    fans = rep.get("fans", []) or []

    # 告警
    if alerts:
        ar = "".join(
            f"<li class='{('danger' if a.get('level')=='danger' else ('warn' if a.get('level')=='warn' else 'ok'))}'>"
            f"[{esc(a.get('level','info'))}] {esc(a.get('title',''))} — {esc(a.get('detail',''))}</li>"
            for a in alerts)
        alert_box = f"<div class='alert-box'><ul>{ar}</ul></div>"
    else:
        alert_box = "<div class='alert-box'><span class='ok'>无活动告警 ✓</span></div>"

    sys_rows = [
        ["CPU 型号", fmt(sys_.get("cpu_model"))],
        ["CPU 核心/线程", f"{fmt(sys_.get('cpu_cores'))} / {fmt(sys_.get('cpu_threads'))}"],
        ["CPU 频率", fmt(sys_.get("cpu_freq"), " MHz")],
        ["负载 (1/5/15)", " / ".join(fmt(x) for x in (sys_.get("load") or []))],
        ["内存", f"{fmt(mem.get('used'))} / {fmt(mem.get('total'))}（{fmt(mem.get('percent'))}%）"],
        ["交换分区", f"{fmt(swap.get('used'))} / {fmt(swap.get('total'))}"],
        ["显卡", fmt("、".join(gpus) if gpus else "—")],
    ]
    board_rows = [
        ["制造商", fmt(board.get("manufacturer"))],
        ["型号", fmt(board.get("product"))],
        ["版本", fmt(board.get("version"))],
        ["BIOS 厂商", fmt(board.get("bios_vendor"))],
        ["BIOS 版本", fmt(board.get("bios_version"))],
        ["BIOS 日期", fmt(board.get("bios_date"))],
        ["芯片组", fmt(board.get("chipset"))],
    ]
    if board.get("note"):
        board_rows.append(["备注", fmt(board.get("note"))])
    mm_rows = []
    for m in (mm.get("modules") or []):
        mm_rows.append([
            fmt(m.get("locator")), "已装" if m.get("installed") else "空槽",
            fmt(m.get("brand")), fmt(m.get("manufacturer")), fmt(m.get("part")),
            fmt(m.get("size")), fmt(m.get("type")), fmt(m.get("speed")),
        ])
    mem_summary = "共 {s} 槽 ｜ 已装 {t} GB ｜ 品牌汇总：{b}".format(
        s=fmt(mm.get("slots")), t=fmt(mm.get("total_gb")), b=fmt(mm.get("brand_summary")))
    temp_rows = [[fmt(t.get("name")), fmt(t.get("value"), " ℃"), fmt(t.get("max"), " ℃"), fmt(t.get("crit"), " ℃")]
                 for t in (sens.get("temps") or [])]
    fan_sens_rows = [[fmt(f.get("name")), fmt(f.get("rpm"), " RPM"), fmt(f.get("mode")), fmt(f.get("pwm"), " %")]
                     for f in (sens.get("fans") or [])]
    volt_rows = [[fmt(v.get("name")), fmt(v.get("value"), " V")] for v in (sens.get("voltages") or [])]
    fanctl_rows = []
    for f in fans:
        fanctl_rows.append([
            fmt(f.get("name")), fmt(f.get("rpm"), " RPM"), fmt(f.get("pwm"), " %"),
            fmt(f.get("mode")), (fmt(f.get("target_pct"), " %") if f.get("target_pct") is not None else "—"),
            fmt(f.get("voltage")),
        ])
    nic_rows = [[fmt(n.get("name")), fmt(n.get("state")), fmt(n.get("mac")), fmt(n.get("speed"), " Mbps"),
                 fmt(n.get("ip")),
                 (f"↓{fmt(n.get('rx_rate'))} ↑{fmt(n.get('tx_rate'))}" if n.get("rx_rate") is not None else "—")]
                for n in nics]
    disk_rows = []
    for d in disks:
        rota = str(d.get("rota"))
        rota_s = "机械盘" if rota == "1" else ("固态" if rota == "0" else fmt(d.get("rota")))
        disk_rows.append([
            fmt(d.get("dev")), fmt(d.get("brand")), fmt(d.get("model")), fmt(d.get("size")),
            fmt(d.get("tran") or d.get("type")), rota_s,
            (fmt(d.get("temp"), " ℃") if d.get("temp") is not None else "—"),
            fmt(d.get("health")),
            (fmt(d.get("power_on_hours"), " h") if d.get("power_on_hours") is not None else "—"),
            (fmt(d.get("reallocated")) if d.get("reallocated") is not None else "—"),
            (fmt(d.get("pending")) if d.get("pending") is not None else "—"),
        ])
    raid_rows = [[fmt(a.get("name")), fmt(a.get("level")), fmt(a.get("state")), fmt(a.get("size")),
                  fmt("、".join(a.get("disks") or []))] for a in (storage.get("raid_arrays") or [])]
    raid_info = "阵列卡：{m}（{mode}）".format(m=fmt(raid.get("model")), mode=fmt(raid.get("mode")))
    if raid.get("note"):
        raid_info += " ｜ " + fmt(raid.get("note"))
    vol_rows = [[fmt(v.get("mount")), fmt(v.get("fstype")), fmt(v.get("size")), fmt(v.get("used")),
                 fmt(v.get("avail")), fmt(v.get("pcent"))] for v in (storage.get("volumes") or [])]
    c_rows = []
    for c in (docker.get("containers") or []):
        c_rows.append([
            fmt(c.get("name")), fmt(c.get("image")), fmt(c.get("status")), fmt(c.get("ports")),
            (fmt(c.get("cpu"), " %") if c.get("cpu") is not None else "—"),
            (fmt(c.get("mem_pct"), " %") if c.get("mem_pct") is not None else "—"),
            (fmt(c.get("mem")) if c.get("mem") else "—"),
            (f"{fmt(c.get('net_rx'))} / {fmt(c.get('net_tx'))}" if c.get("net_rx") is not None else "—"),
        ])
    topology = storage.get("topology", "") or ""

    body = (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>nasdash 硬件健康报告</title><style>" + CSS + "</style></head><body>"
        f"<h1 class='rep-title'>nasdash 硬件健康报告</h1>"
        f"<p class='rep-sub'>报告类型：HTML ｜ 生成时间 <b>{esc(rep.get('generated_at'))}</b> ｜ "
        f"版本 <b>{esc(rep.get('version'))}</b> ｜ 主机 <b>{esc(rep.get('host'))}</b> ｜ "
        f"运行时长 <b>{esc(rep.get('uptime'))}</b></p>"
        # 计算机摘要（AIDA64 风：顶部概览）
        + cat("计算机摘要")
        + props([
            ["计算机名称", sys_.get("hostname")],
            ["操作系统 / 运行时长", fmt(sys_.get("uptime"))],
            ["nasdash 版本", rep.get("version")],
            ["CPU 温度", fmt(sys_.get("cpu_temp"), " ℃")],
            ["内存使用率", fmt(mem.get("percent"), " %")],
            ["硬盘数量", f"{len(disks)} 块"],
            ["阵列卡", ("已识别：" + fmt(raid.get("model"))) if raid.get("model") else "无"],
            ["Docker 容器", f"{docker.get('running',0)} / {docker.get('total',0)} 运行中"],
            ["风扇数量", f"{len(fans)} 个"],
            ["活动告警", f"{len(alerts)} 项"],
        ])
        + cat("活动告警")
        + alert_box
        + cat("系统")
        + props(sys_rows)
        + cat("主板 / BIOS")
        + props(board_rows)
        + cat("内存")
        + note(mem_summary)
        + data(["插槽", "状态", "品牌", "制造商", "部件号", "容量", "类型", "频率"], mm_rows)
        + cat("传感器 — 温度")
        + data(["传感器", "当前", "上限", "临界"], temp_rows)
        + cat("传感器 — 风扇")
        + data(["风扇", "转速", "模式", "占空比"], fan_sens_rows)
        + cat("传感器 — 电压")
        + data(["电压", "值"], volt_rows)
        + cat("风扇控制状态")
        + data(["风扇", "转速", "当前占空比", "模式", "目标占空比", "电压"], fanctl_rows)
        + cat("网卡")
        + data(["名称", "状态", "MAC", "速率", "IP", "实时速率"], nic_rows)
        + cat("硬盘 SMART")
        + data(["设备", "品牌", "型号", "容量", "接口", "类型", "温度", "健康", "通电", "重映射", "待映射"], disk_rows)
        + cat("阵列卡 / RAID")
        + note(raid_info)
        + data(["阵列", "级别", "状态", "容量", "成员盘"], raid_rows)
        + cat("存储卷")
        + data(["挂载点", "文件系统", "总容量", "已用", "可用", "使用率"], vol_rows)
        + cat("Docker 容器")
        + note(f"运行中 {docker.get('running',0)} / 共 {docker.get('total',0)}")
        + data(["名称", "镜像", "状态", "端口", "CPU", "内存%", "内存", "网络 RX/TX"], c_rows)
        + (cat("存储拓扑 (lsblk)")
           + f"<div class='note'><pre style='margin:0;white-space:pre-wrap;font-family:Consolas,Menlo,monospace;font-size:12px'>{esc(topology)}</pre></div>"
           if topology.strip() else "")
        + "<div class='footer'>本报告由 nasdash 自动生成，仅供硬件健康参考。</div>"
        "</body></html>"
    )
    return make_response(body, 200, {"Content-Type": "text/html; charset=utf-8"})

@app.route("/api/alerts")
def api_alerts():
    cfg = _load_alerts()
    system = get_system()
    disks = get_disks()
    docker = get_docker()
    system_full = {**system, "board": get_board(), "memory_modules": get_memory_modules()}
    alerts = _evaluate_alerts(system_full, disks, docker)
    return jsonify({"config": cfg, "alerts": alerts, "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": True})

@app.route("/api/alerts/config", methods=["GET"])
def api_alerts_config_get():
    return jsonify(_load_alerts())

@app.route("/api/alerts/config", methods=["POST"])
@require_admin()
def api_alerts_config_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_alerts()
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if "disk_health" in data:
        cfg["disk_health"] = bool(data["disk_health"])
    if "memory_max" in data:
        try:
            cfg["memory_max"] = float(data["memory_max"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "memory_max 需为数字"}), 400
    if "temp" in data and isinstance(data["temp"], dict):
        t = cfg["temp"]; nt = data["temp"]
        for k in ("cpu_max", "mb_max", "disk_max"):
            if k in nt:
                try:
                    t[k] = float(nt[k])
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": k + " 需为数字"}), 400
        if "enabled" in nt:
            t["enabled"] = bool(nt["enabled"])
    if "channels" in data and isinstance(data["channels"], dict):
        ch = cfg["channels"]; nc = data["channels"]
        for name in ("telegram", "bark", "email"):
            if name in nc and isinstance(nc[name], dict):
                cur = ch.get(name, {})
                for fld in ("enabled", "bot_token", "chat_id", "url", "smtp_host", "smtp_port", "user", "pass", "to"):
                    if fld in nc[name]:
                        cur[fld] = nc[name][fld]
                ch[name] = cur
        if "system" in nc:
            ch["system"] = bool(nc["system"])
    if _save_alerts(cfg):
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": False, "error": "写配置失败"}), 500

@app.route("/api/alerts/test", methods=["POST"])
@require_admin()
def api_alerts_test():
    cfg = _load_alerts()
    text = "这是一条来自 nasdash 的测试通知（当前版本 %s）。若收到说明通知渠道配置正确。" % APP_VERSION
    res = _dispatch_notifications(text, cfg)
    return jsonify({"ok": True, "results": res})

@app.route("/api/report")
def api_report():
    fmt = request.args.get("format", "json")
    rep = build_health_report()
    if fmt == "html":
        return _render_report_html(rep)
    return jsonify(rep)

# ===================== 前端 =====================
def _serve_gateway(app, socket_path):
    """统一网关模式：在标准库 wsgiref 上监听 Unix Socket。
    不用 Flask app.run(unix_socket=) 的原因：新版 Werkzeug(>=2.1) 已移除该参数，而 wsgiref
    是 Python 标准库，与 Flask/Werkzeug 版本无关，在飞牛 fnOS 上必定可用。飞牛网关会先校验
    NAS 登录态，再把请求转发到本 Socket。"""
    import os as _os, socket as _socket, socketserver as _ss
    from wsgiref.simple_server import WSGIServer, WSGIRequestHandler

    socket_path = _os.path.abspath(socket_path)
    parent = _os.path.dirname(socket_path)
    if parent:
        _os.makedirs(parent, exist_ok=True)
    try:
        if _os.path.exists(socket_path):
            _os.unlink(socket_path)
    except OSError:
        pass

    class _UnixWSGIServer(_ss.ThreadingMixIn, WSGIServer):
        address_family = _socket.AF_UNIX
        daemon_threads = True
        def server_bind(self):
            self.socket.bind(self.server_address)
            self.socket.listen(self.request_queue_size)
            self.server_name = "localhost"
            self.server_port = 0
            # WSGIServer.server_bind 原本会调 setup_environ() 生成 base_environ，
            # 这里重写了 server_bind，需手动补上，否则请求处理时取 base_environ 会报错。
            self.setup_environ()

    class _H(WSGIRequestHandler):
        def address_string(self):
            return "localhost"
        def setup(self):
            # Unix Socket 的 client_address 是空字符串 ''，会导致 wsgiref 的
            # make_environ() 访问 client_address[0] 时 IndexError；此处修正为合法元组。
            self.client_address = ("127.0.0.1", 0)
            super().setup()
        def log_message(self, *a, **k):
            pass

    srv = _UnixWSGIServer(socket_path, _H)
    srv.set_app(app)
    # 网关进程以其他用户身份连接本 socket，需放开连接权限。
    # 相比旧版 0.0.0.0:9800 对全网开放且无任何鉴权，现收敛到仅经飞牛登录态校验后才能连的
    # 本地 socket，安全性反而更高；本地 socket 只需同机进程可达，无需担心跨网络暴露。
    try:
        _os.chmod(socket_path, 0o777)
    except OSError:
        pass
    try:
        srv.serve_forever()
    finally:
        try:
            _os.unlink(socket_path)
        except OSError:
            pass


if __name__ == "__main__":
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    # 统一网关模式（飞牛 fnOS 应用中心）：监听 Unix Socket，网关先校验 NAS 登录态再转发。
    # 由 cmd/main 注入 NAS_DASH_GATEWAY=1 启用；本地开发/CI 不设则保持 TCP 端口（便于测试）。
    if os.environ.get("NAS_DASH_GATEWAY") == "1":
        socket_path = os.environ.get("APP_SOCKET") or os.path.join(APP_DIR, "app.sock")
        # 网关转发请求路径带前缀 /app/{appname}，去掉前缀再交给原路由。
        _gw_prefix = (os.environ.get("GATEWAY_PREFIX") or "/app/com.dashboard.nasdash").rstrip("/")
        if _gw_prefix and _gw_prefix != "/":
            # 标准 Flask 中间件写法：包裹已有的 app.wsgi_app（Flask 的 WSGI 处理器），
            # 而非 app 本身——否则 app.__call__ 会再次调回 app.wsgi_app 形成无限递归。
            class _PrefixMiddleware:
                def __init__(self, wsgi_app):
                    self.wsgi_app = wsgi_app
                def __call__(self, environ, start_response):
                    path = environ.get("PATH_INFO", "")
                    if path == _gw_prefix or path.startswith(_gw_prefix + "/"):
                        environ["PATH_INFO"] = path[len(_gw_prefix):] or "/"
                        environ["SCRIPT_NAME"] = _gw_prefix
                    return self.wsgi_app(environ, start_response)
            app.wsgi_app = _PrefixMiddleware(app.wsgi_app)
        # 同时监听 127.0.0.1:TRIM_SERVICE_PORT（由 manifest service_port 注入），
        # 供飞牛网关按端口转发；仅绑定本地回环，不对外暴露。网关既可通过
        # app.sock 也可通过 127.0.0.1:service_port 访问，兼容 fygo-browser/app-cleaner/Hermes 三种转发模式。
        _service_port = (os.environ.get("TRIM_SERVICE_PORT") or "").strip()
        if _service_port:
            def _serve_tcp(app, port):
                from wsgiref.simple_server import make_server
                srv = make_server("127.0.0.1", port, app)
                srv.serve_forever()
            _port = int(_service_port)
            _tcp_thread = _threading.Thread(target=_serve_tcp, args=(app, _port), daemon=True)
            _tcp_thread.start()
        _serve_gateway(app, socket_path)
    else:
        _env_port = (os.environ.get("TRIM_SERVICE_PORT") or "").strip()
        port = int(_env_port) if _env_port else 9800
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
