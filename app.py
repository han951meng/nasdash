#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 硬件监控面板 (fnOS Hardware Dashboard)
单文件 Flask 应用：阵列卡状态 / 硬盘 SMART / 系统资源 / 存储卷
部署目录: /opt/fnos-dash/
"""
import subprocess, json, re, os, time, socket, signal, platform, shutil, sys, glob, functools, errno, urllib.request, urllib.error, base64
from flask import Flask, jsonify, render_template, render_template_string, request, make_response, Response, stream_with_context, send_from_directory
try:
    from markupsafe import Markup
except Exception:
    Markup = str
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
    for name in ("fan_labels.json", "fan_disk_temp.json", "fan_sys_temp.json"):
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

# 版本号单一来源：fnOS 标准安装时 manifest 不在 APP_DIR（APP_DIR 只有 app.tgz 内容），
# 而是在 /var/apps/<appid>/manifest；热替换部署时我们又会把 manifest 上传到 APP_DIR。
# 两个位置都查，取「版本号较高者」——既兼容标准安装（只有 /var/apps 有），
# 也兼容热替换（APP_DIR 已是最新、/var/apps 仍是旧版），最后才回退硬编码值。
def _parse_ver(v):
    try:
        s = str(v).strip().lstrip("vV")
        return tuple(int(x) for x in s.split("."))
    except Exception:
        return (0,)

def _app_version():
    appid = os.path.basename(APP_DIR)  # 如 com.dashboard.nasdash
    candidates = [
        os.path.join(APP_DIR, "manifest"),
        os.path.join("/var/apps", appid, "manifest"),
    ]
    best = None
    for path in candidates:
        try:
            with open(path) as f:
                m = re.search(r"^version\s*=\s*(\S+)", f.read(), re.M)
                if m:
                    ver = "v" + m.group(1).strip()
                    t = _parse_ver(ver)
                    if best is None or t > best[0]:
                        best = (t, ver)
        except Exception:
            pass
    return best[1] if best else "v1.6.2"
APP_VERSION = _app_version()

def _load_icon_data(name):
    """把 ui/images/ 下的 PNG 图标读成 base64 data URL，内嵌到页面里避免网关静态资源 302 问题。
    返回 Markup 对象，避免 Jinja2 autoescape 在 JS/HTML 里把 data URL 转义成可见文本。"""
    try:
        path = os.path.join(os.path.dirname(__file__), "ui", "images", name)
        with open(path, "rb") as f:
            return Markup("data:image/png;base64," + base64.b64encode(f.read()).decode("ascii"))
    except Exception:
        return Markup("")
# 左侧导航与面板大图标：统一采用用户提供的彩色插画图标，base64 内嵌避免网关静态资源 302 问题。
ICON_DETECT_DATA = _load_icon_data("icon-detect.png")
ICON_SYSTEM_DATA = _load_icon_data("icon-system.png")
ICON_HISTORY_DATA = _load_icon_data("icon-history.png")
ICON_RAID_DATA = _load_icon_data("icon-raid.png")
ICON_HDD_DATA = _load_icon_data("icon-hdd.png")
ICON_STORAGE_DATA = _load_icon_data("icon-storage.png")
ICON_FAN_DATA = _load_icon_data("icon-fan.png")
ICON_DOCKER_DATA = _load_icon_data("icon-docker.png")
ICON_AUTOMATION_DATA = _load_icon_data("icon-automation.png")
ICON_MANUAL_DATA = _load_icon_data("icon-manual.png")
ICON_ABOUT_DATA = _load_icon_data("icon-about.png")

def _fnos_version():
    """读取 fnOS 系统版本。优先从 /usr/trim/etc/version 读取；取不到再回退 os-release。"""
    try:
        if os.path.exists("/usr/trim/etc/version"):
            with open("/usr/trim/etc/version", encoding="utf-8", errors="ignore") as f:
                ver = f.read().strip()
            if ver:
                return "fnOS " + ver
    except Exception:
        pass
    for path in ("/usr/os-release", "/etc/fnos-release", "/etc/os-release"):
        try:
            with open(path) as f:
                txt = f.read()
            m = re.search(r"PRETTY_NAME=\"?([^\"\n]*fnOS[^\"\n]*)", txt, re.I)
            if not m:
                m = re.search(r"VERSION=\"?([^\"\n]*fnOS[^\"\n]*)", txt, re.I)
            if m:
                return m.group(1).strip()
            if path == "/etc/os-release":
                mv = re.search(r"VERSION_ID=\"?([^\"\n]*)", txt)
                if mv:
                    return "Debian " + mv.group(1).strip() + " (fnOS 底层)"
        except Exception:
            pass
    return "未知"

def _debug_log_tail(n=60):
    """读取应用 debug.log 末尾 n 行（脱敏：卷路径简化为占位，避免泄露用户目录结构）。"""
    try:
        with open(os.path.join(APP_DIR, "debug.log"), "r", errors="ignore") as f:
            lines = f.readlines()
        tail = lines[-n:] if len(lines) > n else lines
        redacted = []
        for ln in tail:
            ln = re.sub(r"/vol\d+/@\S+", "<卷路径>", ln)
            redacted.append(ln.rstrip("\n"))
        return redacted
    except Exception:
        return []

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
BADBLOCKS = "/usr/sbin/badblocks"
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

def _run_raw(args, timeout=30, as_root=False, quiet=False):
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
        if not quiet and r.returncode != 0 and r.stderr.strip():
            log("cmd failed (rc=%d): %s\n%s" % (r.returncode, " ".join(args), r.stderr.strip()))
        return r.stdout
    except Exception as e:
        log("cmd error: %s\n%s" % (" ".join(map(str, args)), e))
        return ""

def run_cmd(args, timeout=30, quiet=False):
    """shell=False 执行（推荐）：args 为参数列表，不接受字符串。"""
    return _run_raw(args, timeout, as_root=False, quiet=quiet)

def sudo_cmd(args, timeout=30, quiet=False):
    """shell=False 执行需 root 的命令（自动 passwordless sudo 兜底）。"""
    return _run_raw(args, timeout, as_root=True, quiet=quiet)

# ===================== JSON 配置读写（统一）=====================
def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        # default=None 作哨兵时不做类型校验（否则 isinstance(dict, NoneType) 恒为 False，
        # 会把已存的配置误判丢弃，导致 disk_temp/sys_temp 配置永远读不回来）；
        # 传具体类型（如 {}）的调用者仍保留类型校验，非期望类型时回退默认值。
        if default is None:
            return d
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

# ===================== 真实 CPU 使用率（固定 1s 窗口 + 缓存，杜绝毫秒级窗口尖刺）=====================
# 设计要点：
#  - 后台 daemon 每 ~1s 计算一次真实值写进 _CPU_USAGE_CACHE（低延迟，请求直接读缓存）。
#  - 若 daemon 异常超过 2s 未刷新（兜底），get_cpu_usage 就地做「真实睡眠 1s 的两次采样」，
#    保证采样窗口永远是 ~1s，而不是被并发请求压成毫秒级 → 不再出现瞬时 100%/50%/0% 尖刺。
#  - 并发请求在 1s 内共享同一份缓存值，不会各自开一个微小窗口导致数值乱跳。
_CPU_USAGE_CACHE = {"v": None, "t": 0.0, "prev": None}   # v=最新使用率, t=写入时间, prev=上一次 /proc/stat(total,idle)
_CPU_USAGE_LOCK = _threading.Lock()

def _cpu_snap():
    """读 /proc/stat 首行聚合，返回 (idle, total)。idle 含 iowait，total 含 user..steal。"""
    parts = read_file("/proc/stat", "").split('\n', 1)[0].split()
    cols = list(map(int, parts[1:]))
    idle = cols[3] + cols[4]          # idle + iowait
    total = sum(cols[:8])             # user..steal（不含 guest，已计入 user）
    return idle, total

def _cpu_sample_1s():
    """兜底采样：真实睡眠 1s 的两次 /proc/stat 读数，算 1s 窗口使用率。返回 0-100 浮点或 None。"""
    try:
        i1, t1 = _cpu_snap()
        time.sleep(1.0)
        i2, t2 = _cpu_snap()
        d_total = t2 - t1
        if d_total <= 0:
            v = 0.0
        else:
            v = (1.0 - (i2 - i1) / d_total) * 100.0
        v = max(0.0, min(100.0, round(v, 1)))
        with _CPU_USAGE_LOCK:
            _CPU_USAGE_CACHE["v"] = v
            _CPU_USAGE_CACHE["t"] = time.time()
        return v
    except Exception:
        return None

def get_cpu_usage():
    """真实 CPU 使用率（%）：优先返回后台 daemon 每 ~1s 刷新的缓存值（低延迟）；
    若缓存 >2s 未更新（daemon 异常）则就地 1s 窗口采样兜底。永不返回毫秒级窗口的尖刺值。"""
    try:
        now = time.time()
        with _CPU_USAGE_LOCK:
            c = dict(_CPU_USAGE_CACHE)
        if c["v"] is not None and (now - c["t"]) < 2.0:
            return c["v"]
        return _cpu_sample_1s()
    except Exception:
        return None


# 全局风扇目标状态：key=(hwmon, idx) -> {"mode":"manual"|"auto", "target":0-255}
FAN_LOCK = _threading.Lock()
FAN_TARGETS = {}
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

# 接管总开关关闭时落到的「安全待机占空比」：保持 enable=1 手动模式，不写 2。
# 原因：NCT6797 上 pwm_enable=2（交还芯片 Thermal Cruise）会把风扇拉到满速狂转；
# 这块 DIY 主板又无 fnOS 原生 FCS 可接管，故只能由 nasdash 以手动模式落一个安静待机值并停手，
# 让用户明确看到「接管已释放、风扇已放慢」，而不是冻结在高位看着像还在控。
_FAN_RELEASE_IDLE_PWM = 30

def _fan_release_to_idle():
    """接管总开关关闭时：把每个「真实在转」的风扇降到安全待机占空比并停手。
    跳过无转速反馈的口（如水泵 / 真空口 / 未接风扇，fanN_input 读 0），避免误把水泵降到低速影响散热。
    不写 pwm_enable=2（NCT6797 上 Thermal Cruise 会狂转），详见上方常量注释。"""
    for (hwmon, idx) in _enumerate_fans():
        try:
            try:
                with open(f"{hwmon}/fan{idx}_input") as _f:
                    if int((_f.read().strip() or 0)) <= 0:
                        continue
            except Exception:
                pass
            _fan_write_raw(hwmon, idx, _FAN_RELEASE_IDLE_PWM)
        except Exception:
            pass

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
# systemctl start/stop pwm-fancontrol 可能慢至数秒，用锁+后台线程避免阻塞 HTTP 请求。
_FCS_OP_LOCK = _threading.Lock()

def _fcs_disabled():
    """用户是否已在面板永久禁用 FanControlServer（读持久化标志）。"""
    return bool(_load_json_file(FCS_STATE_FILE, {}).get("disabled"))

def _set_fcs_disabled(v):
    return _save_json_file(FCS_STATE_FILE, {"disabled": bool(v)})

def _fcs_installed_state():
    """systemctl is-enabled 的原始结果（enabled/disabled/masked/static/...；未安装为空串）。"""
    return run_cmd(["systemctl", "is-enabled", "pwm-fancontrol"], 3).strip().lower()

def _fcs_status():
    """汇总 FanControlServer 状态供面板展示：是否安装/是否开机自启/是否在跑/是否被用户永久禁用。
    用一条 `systemctl show` 同时取 ActiveState 与 UnitFileState，比 is-active + is-enabled
    两次独立命令少一次 D-Bus 往返，降低超时/卡住的概率。"""
    raw = run_cmd(["systemctl", "show", "pwm-fancontrol",
                   "-p", "ActiveState", "-p", "UnitFileState", "--no-pager"], 4).strip()
    active = ""
    unit_state = ""
    for line in raw.splitlines():
        if line.startswith("ActiveState="):
            active = line.split("=", 1)[1].strip().lower()
        elif line.startswith("UnitFileState="):
            unit_state = line.split("=", 1)[1].strip().lower()
    installed = bool(unit_state) or active in ("active", "activating")
    enabled = unit_state == "enabled"
    running = active in ("active", "activating")
    return {
        "installed": installed,
        "enabled": enabled,
        "running": running,
        "disabled_by_user": _fcs_disabled(),
        "raw": unit_state or active,
    }

# 诊断开关：置 True 时跳过 systemctl，直接返回硬编码状态，用于排查网关/请求路径问题。
_FCS_DIAG_HARD = False

def _fcs_status_hard():
    return {"installed": False, "enabled": False, "running": False,
            "disabled_by_user": False, "raw": "diag"}

# FCS 状态查询涉及 systemctl，可能耗时 1~3 秒；面板高频刷新/用户快速切页时会并发请求。
# 改为后台线程每隔 TTL 刷新一次，HTTP 接口只读缓存，永远不再因为 systemctl 慢而卡住请求。
_FCS_STATUS_CACHE = {"t": 0.0, "v": None, "lock": _threading.Lock()}
_FCS_STATUS_TTL = 15.0

def _fcs_status_refresh_loop():
    while True:
        try:
            v = _fcs_status_hard() if _FCS_DIAG_HARD else _fcs_status()
            with _FCS_STATUS_CACHE["lock"]:
                _FCS_STATUS_CACHE["v"] = v
                _FCS_STATUS_CACHE["t"] = time.time()
        except Exception:
            pass
        time.sleep(_FCS_STATUS_TTL)

def _fcs_status_cached(clear=False):
    """返回 FCS 状态缓存；首次调用会启动后台刷新线程，之后 always 立即返回。
    切换开关等操作后调用 clear=True 会先尝试同步刷新一次，让用户立刻看到最新状态。"""
    if _FCS_DIAG_HARD:
        return _fcs_status_hard()
    cache = _FCS_STATUS_CACHE
    with cache["lock"]:
        if not getattr(_fcs_status_cached, "_started", False):
            _threading.Thread(target=_fcs_status_refresh_loop, daemon=True).start()
            _fcs_status_cached._started = True
        if clear:
            cache["t"] = 0.0
            cache["v"] = None
        now = time.time()
        if cache["v"] is not None and now - cache["t"] < _FCS_STATUS_TTL:
            return cache["v"]
    # clear 时（如用户点了禁用/恢复）同步刷一次，保证 UI 即时反馈；_fcs_status 内部有 4s 超时兜底。
    if clear:
        v = _fcs_status()
        with cache["lock"]:
            cache["v"] = v
            cache["t"] = time.time()
        return v
    return {"installed": False, "enabled": False, "running": False,
            "disabled_by_user": _fcs_disabled(), "raw": ""}

def _fcs_has_board_config():
    """判断 FCS 是否真的配置了风扇参数。飞牛部分机型 /boot/board.json 为空或没有 fan 段，
    此时启动 pwm-fancontrol 只是空跑 RemainAfterExit，并不会接管风扇。"""
    try:
        with open("/boot/board.json") as _fh:
            _d = _json.load(_fh)
        _fans = _d.get("fan") if isinstance(_d, dict) else None
        return isinstance(_fans, list) and len(_fans) > 0
    except Exception:
        return False

def _fan_stop_ext_service():
    """临时停止系统风扇服务 FanControlServer（接管窗口内，仅 best-effort）。
    后台线程执行，避免 systemctl stop 数秒阻塞 HTTP/主控循环。"""
    def _do_stop():
        with _FCS_OP_LOCK:
            try:
                sudo_cmd(["systemctl", "stop", "pwm-fancontrol"], 5)
            except Exception:
                pass
            try:
                sudo_cmd(["pkill", "-f", "pwm-fancontrol"], 2)
            except Exception:
                pass
    _threading.Thread(target=_do_stop, daemon=True).start()

def _fan_start_ext_service():
    """交还自动时重启系统风扇服务 FanControlServer（仅 best-effort）。
    若用户已在面板「永久禁用」FCS，则不再拉起，尊重用户选择。
    后台线程执行，避免 systemctl start 数秒阻塞 HTTP/主控循环。"""
    if _fcs_disabled():
        return
    def _do_start():
        with _FCS_OP_LOCK:
            try:
                sudo_cmd(["systemctl", "start", "pwm-fancontrol"], 5)
            except Exception:
                pass
    _threading.Thread(target=_do_start, daemon=True).start()

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

# ===================== 统一温度采集（一次采样、处处共享） =====================
# 历史问题：CPU/主板/温度墙/控速线程各自跑 sensors -j、各带各的缓存，导致
# ① coretemp 的 temp1_input（驱动虚拟偏高读数）被当 CPU 温度，虚高 ~10°C；
# ② 控速线程每 0.6s 跑一次 sensors；③ 温度墙 30s 才刷新，显示不及时。
# 统一方案：后台采集循环 ~2s 一次 sensors -j → 一份 _TEMP_SNAP 快照；
# 所有温度消费方（/api/fan/temps、get_system、控速线程、get_fan_status）读同一份快照。
# 硬盘(4s TTL)与阵列卡(12~60s TTL)走各自已有缓存并入快照。

def _parse_cpu_temp(j):
    """统一 CPU 封装温度解析（纯函数，不跑命令）。
    1) AMD：k10temp/zenpower 的 Tdie（真实量纲）优先，Tctl 次之；
    2) Intel coretemp：封装温度 = 各核心(Core N)测点最大值，排除 temp1_input
       （coretemp 驱动的"虚拟最高点"读数，曾致 CPU 温度虚高 ~10°C）；
    3) 兜底：任意 Package/Tdie/Tctl 标签取 max（coretemp 仍排除 temp1_input）。"""
    j = j or {}
    for chip, entries in j.items():
        c = str(chip).lower()
        if c.startswith("k10temp") or c.startswith("zenpower"):
            best = None
            for ename, fields in (entries or {}).items():
                if not isinstance(fields, dict):
                    continue
                en = str(ename).lower()
                if "tdie" in en:
                    for k, v in fields.items():
                        if k.endswith("_input") and isinstance(v, (int, float)):
                            return float(v)
                if "tctl" in en and best is None:
                    for k, v in fields.items():
                        if k.endswith("_input") and isinstance(v, (int, float)):
                            best = float(v)
            if best is not None:
                return best
    core_vals, pkg_vals = [], []
    for chip, entries in j.items():
        if not str(chip).lower().startswith("coretemp"):
            continue
        for ename, fields in (entries or {}).items():
            if not isinstance(fields, dict):
                continue
            en = str(ename)
            vals = [float(v) for k, v in fields.items()
                    if k.startswith("temp") and k.endswith("_input")
                    and k != "temp1_input" and isinstance(v, (int, float))]
            if re.match(r"^Core\s+\d+$", en):
                core_vals.extend(vals)
            elif "package" in en.lower():
                pkg_vals.extend(vals)
    if core_vals:
        return max(core_vals)
    if pkg_vals:
        return max(pkg_vals)
    fallback = []
    for chip, entries in j.items():
        for ename, fields in (entries or {}).items():
            if not isinstance(fields, dict):
                continue
            en = str(ename).lower()
            if "package" in en or "tdie" in en or "tctl" in en:
                for k, v in fields.items():
                    if k.startswith("temp") and k.endswith("_input") and isinstance(v, (int, float)):
                        if str(chip).lower().startswith("coretemp") and k == "temp1_input":
                            continue
                        fallback.append(float(v))
    return max(fallback) if fallback else None

def _parse_mb_temp(j):
    """统一主板温度解析（纯函数）：SYSTIN 精确测点优先；
    回落排除 coretemp/AUX 后最高；再回落非 coretemp 最高。"""
    temps = []
    for chip, entries in (j or {}).items():
        if not isinstance(entries, dict):
            continue
        for ename, fields in entries.items():
            if not isinstance(fields, dict) or ename == "Adapter":
                continue
            for k, v in fields.items():
                if k.startswith("temp") and k.endswith("_input") and isinstance(v, (int, float)):
                    temps.append((str(chip), str(ename), float(v)))
    if not temps:
        return None
    systin = [t for t in temps if t[1].strip().upper() == "SYSTIN"]
    if systin:
        return systin[0][2]
    mb = [t for t in temps if "coretemp" not in t[0].lower() and not t[1].strip().upper().startswith("AUXTIN")]
    if mb:
        return max(t[2] for t in mb)
    mb2 = [t for t in temps if "coretemp" not in t[0].lower()]
    if mb2:
        return max(t[2] for t in mb2)
    return max(t[2] for t in temps)

def _parse_sensors_all(j):
    """统一传感器全量解析（温度墙测点 + 电压）：与旧 get_system 内联解析同口径，
    抽成纯函数供采集循环与 get_system 共用，保证两处数据完全一致。"""
    temps, voltages = [], []
    for chip, entries in (j or {}).items():
        cs = str(chip).split("-")[0]
        for ename, fields in (entries or {}).items():
            if not isinstance(fields, dict) or ename == "Adapter":
                continue
            for fn, fv in fields.items():
                if not fn.endswith("_input") or not isinstance(fv, (int, float)):
                    continue
                prefix = fn.replace("_input", "")
                if fn.startswith("temp"):
                    if str(ename).strip().upper().startswith("AUXTIN"):
                        continue
                    if fv < -50 or fv > 150:
                        continue
                    mx = fields.get(prefix + "_max")
                    cr = fields.get(prefix + "_crit")
                    if cs != "coretemp":
                        mx = cr = None
                    if mx is not None and (mx < 0 or mx > 150):
                        mx = None
                    if cr is not None and (cr < 0 or cr > 150):
                        cr = None
                    nm = _temp_name_zh(ename, cs) if cs == "coretemp" else (
                        "主板(ACPI)" if cs == "acpitz" else
                        "PCH 芯片组" if cs.startswith("pch") else
                        "主板(CPU附近)" if cs.startswith("it") and "temp1" in str(ename) else
                        "主板(系统)" if cs.startswith("it") and "temp2" in str(ename) else
                        "主板" if cs.startswith("it") else _temp_name_zh(ename, cs))
                    if cs == "coretemp" and "package" in str(ename).lower():
                        # 系统温度合集归口：CPU 封装温度的权威来源只有 _parse_cpu_temp（核心 max、排除
                        # temp1_input 虚拟偏高值）。温度墙此条目直接引用该值，不再自己取 Package 字段，
                        # 否则会与 hero/三页 CPU 温度出现第二套语义差（曾差 2°C）。
                        fv = _parse_cpu_temp(j)
                        if mx is None:
                            for _k, _v in fields.items():
                                if _k.startswith("temp") and _k.endswith("_max") and _k != "temp1_max" and isinstance(_v, (int, float)):
                                    mx = _v
                                    break
                        if cr is None:
                            for _k, _v in fields.items():
                                if _k.startswith("temp") and _k.endswith("_crit") and _k != "temp1_crit" and isinstance(_v, (int, float)):
                                    cr = _v
                                    break
                    temps.append({"name": nm, "raw": str(ename), "value": int(float(fv) + 0.5), "max": mx, "crit": cr})
                    break
                elif fn.startswith("in"):
                    v = fv / 1000 if fv > 100 else fv
                    nm = str(ename)
                    if "3.3V" in nm:
                        nm = "+3.3V"
                    elif "VSB" in nm:
                        nm = "3VSB 待机"
                    elif "bat" in nm.lower():
                        nm = "CMOS 电池"
                    voltages.append({"name": nm, "value": round(v, 2)})
                    break
    return temps, voltages

_TEMP_SNAP = {"t": 0.0, "cpu_temp": None, "mb_temp": None, "temps": [], "voltages": [], "disks": {}, "raid_temp": None}
_TEMP_SNAP_LOCK = _threading.Lock()

# CPU 温度 EMA 平滑状态：核心温度热容量小，瞬时负载下 1~2 秒可跳 10°C+（如 39→47），
# 显示与风扇控速都不宜追着尖峰跑。用指数加权平均（前值 50% + 新值 50%，采集周期 2s）
# 让温度渐进变化；风扇温控同源，转速也更稳。进程内状态，重启后首个读数直接采纳。
_CPU_TEMP_EMA = {"v": None}

def _smooth_cpu_temp(raw):
    if raw is None:
        return _CPU_TEMP_EMA["v"]   # 读不到时沿用上次（避免闪 None/空）
    e = _CPU_TEMP_EMA["v"]
    if e is None:
        e = float(raw)
    else:
        e = e * 0.5 + float(raw) * 0.5
    _CPU_TEMP_EMA["v"] = e
    return int(e + 0.5)   # 显示取整（四舍五入）；EMA 内部保留 float 精度

def _temp_snapshot_read():
    with _TEMP_SNAP_LOCK:
        return dict(_TEMP_SNAP)

def _temp_collect_loop():
    """统一温度采集循环（~2s）：一次 sensors -j 解析出 CPU/主板/全量测点，
    硬盘走 4s 缓存、阵列卡走 12~60s 缓存，汇总成一份快照。
    所有温度消费方读同一份快照，消除重复采样与口径不一致。"""
    while True:
        try:
            sens_j = run_cmd([SENSORS, "-j"], 8)
            j = json.loads(sens_j) if sens_j else {}
            temps, voltages = _parse_sensors_all(j)
            mb = _parse_mb_temp(j)
            # CPU 封装温度：EMA 平滑（防核心瞬时尖峰秒跳）。快照 cpu_temp 与温度墙
            # "CPU 封装温度"条目写同一个平滑值，两处保持一致；风扇温控同源转速更稳。
            raw_cpu = _parse_cpu_temp(j)
            cpu = _smooth_cpu_temp(raw_cpu)
            if isinstance(cpu, (int, float)):
                for _t in temps:
                    if _t.get("name") == "CPU 封装温度":
                        _t["value"] = int(cpu + 0.5)
                        break
            devs = _list_all_disk_devs()
            states = get_disk_temps_cached(devs) if devs else {}
            raid_temp = None
            try:
                _raid = get_raid_card()
                if isinstance(_raid, dict):
                    raid_temp = _raid.get("controller_temp")
            except Exception:
                pass
            with _TEMP_SNAP_LOCK:
                _TEMP_SNAP["t"] = time.time()
                _TEMP_SNAP["cpu_temp"] = cpu
                _TEMP_SNAP["mb_temp"] = int(mb + 0.5) if isinstance(mb, (int, float)) else None
                _TEMP_SNAP["temps"] = temps
                _TEMP_SNAP["voltages"] = voltages
                _TEMP_SNAP["disks"] = states
                _TEMP_SNAP["raid_temp"] = raid_temp
        except Exception:
            pass
        time.sleep(2)

def _fan_cpu_temp_cached():
    """CPU 封装温度（统一快照，~2s 刷新；已修正 coretemp temp1_input 虚高问题）。"""
    return _temp_snapshot_read().get("cpu_temp")

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
            if cf != "all" and [hwmon, idx] not in cf:
                continue
            disk_claimed.add((hwmon, idx))
    # 解耦互斥：sys 与 disk 各自独立选风扇组（不再「sys 先全拿、disk 拿剩余」）。
    # 重叠时 disk 优先（硬盘温控更敏感，是用户明确诉求），从 sys 移除 disk 已选的风扇。
    sys_claimed -= disk_claimed
    return sys_claimed, disk_claimed

# ===================== 风扇接管总开关 =====================
# 关闭后 nasdash 不再根据温度自动调速。若飞牛自带风扇服务已配置，会交还它接管；
# 否则保持当前转速、不再写 PWM。
FAN_CONTROL_CFG = os.path.join(_config_dir(), "fan_control.json")
_FAN_CTRL_ENABLED = True

def _load_fan_control_enabled():
    global _FAN_CTRL_ENABLED
    try:
        d = _load_json_file(FAN_CONTROL_CFG, None)
        if isinstance(d, dict) and "enabled" in d:
            _FAN_CTRL_ENABLED = bool(d["enabled"])
    except Exception:
        pass
    return _FAN_CTRL_ENABLED

_load_fan_control_enabled()

def _restore_fan_modes():
    """nasdash 启动后自动恢复风扇模式，避免重启后 FAN_TARGETS 空导致风扇保持硬件全速：
    - 有持久化配置：按用户上次选择恢复（auto 接管；若 FCS 在控则交还 enable=2；manual 恢复固定值）。
    - 无配置（首次/清配置）：若系统风扇服务 FCS 未在控，默认将所有可控风扇设为 auto 接管，
      消除开机全速；若 FCS 在控则交还、不强行接管（尊重 fnOS 原生控温）。"""
    try:
        if not _FAN_CTRL_ENABLED:
            # 接管关闭：优先把控制权交还 fnOS 原生 FanControlServer（FCS）。
            # FCS 使用 enable=1 + pwm 手动控速；若我们再写 enable=2 会覆盖它，导致风扇被卡在高速。
            # 若 FCS 真的配置了风扇参数，启动它接管；否则 nasdash 不再写 PWM，
            # 保持风扇当前手动值（避免部分主板 enable=2 反而把转速拉得更高）。
            if not _fcs_disabled() and _fcs_installed_state() == "enabled" and _fcs_has_board_config():
                _fan_start_ext_service()
            else:
                # 无 FCS 可接管：落到安全待机占空比，避免开机即高位冻结/狂转。
                try:
                    _fan_release_to_idle()
                except Exception:
                    pass
            return
        enum = _enumerate_fans()
        idx2hw = {i: h for (h, i) in enum}
        if not idx2hw:
            return
        # FCS 服务 active(exited) 不代表它真的能控速：/boot/board.json 为空时它只是空跑。
        # 只有服务在跑且配置了风扇参数时，才视为「系统在控」，nasdash 不抢。
        fcs = _fan_ext_service_running() and _fcs_has_board_config()
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
        if not _FAN_CTRL_ENABLED:
            return
        # 同上：FCS 没真配置时不视为「系统在控」，nasdash 该接管就接管，否则风扇会全速。
        if _fan_ext_service_running() and _fcs_has_board_config():
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
            if not _FAN_CTRL_ENABLED:
                # 接管关闭：优先把控制权交还 fnOS 原生 FanControlServer（FCS）。
                # FCS 使用 enable=1 + pwm 手动控速；若我们再写 enable=2 会覆盖 FCS，
                # 使风扇被卡在最后的高位。因此 FCS 真的配置了风扇参数时只启动/保持它，
                # 不再写 enable=2；否则 nasdash 直接停手，保持风扇当前手动值，
                # 避免某些主板 enable=2 后转速反而被拉得更高。
                if not _fcs_disabled() and _fcs_installed_state() == "enabled" and _fcs_has_board_config():
                    if _FCS_TAKEN["v"]:
                        try: _fan_start_ext_service(); _FCS_TAKEN["v"] = False
                        except Exception: pass
                    if not _fan_ext_service_running():
                        _fan_start_ext_service()
                time.sleep(0.6)
                continue
            # 自愈：每轮确保本机枚举到的每个可控风扇都被接管为 auto（除非用户显式设为 manual）。
            # 解决硬重启时序（hwmon 晚于 nasdash 自启注册、启动期枚举不全）与 fan_mode.json 不完整
            # 导致的「部分风扇失控、开机狂转」。FCS 在控时不抢（交还原生控温）。
            _fan_ensure_all_claimed()
            with FAN_LOCK:
                overrides = dict(FAN_TARGETS)   # 每风扇手动/自动覆盖（仅用户经 UI 调过的风扇）
            all_fans = _enumerate_fans()          # 本机真实风扇全集（it87/nct）
            dt = _load_fan_disk_temp()            # 硬盘监控设置（监控盘 + 休眠/空闲停转，供 disk 源共享）
            rules = _effective_fan_rules()        # 逐风扇温控规则（旧全局面板派生 + 用户逐风扇覆盖）
            controlled = set()
            controlling_any = False   # 本周期是否真正在写 PWM（接管 FCS 的依据）
            # 逐风扇温控：每台风扇按各自「温度源 + 曲线」独立决策（论坛需求：同温源不同曲线）。
            # 温度源每种只算一次，多台同源风扇复用（硬盘温度采集较重，避免重复扫盘）。
            need_disk = any((r.get("source", "disk") == "disk") for r in rules.values())
            _need_combo = any((r.get("source") or "").startswith("combo") for r in rules.values())
            need_cpu = any((r.get("source") == "cpu") for r in rules.values()) or _need_combo
            need_mb = any((r.get("source") == "mb") for r in rules.values()) or _need_combo
            disk_all_idle, disk_T, disk_has = (False, None, False)
            if need_disk:
                _dt_eff = dt
                if not dt.get("disks"):
                    # 逐风扇 disk 规则不依赖全局监控盘白名单：自动用本机全部盘作温度源
                    _all_devs = _list_all_disk_devs()
                    if _all_devs:
                        _dt_eff = dict(dt, disks=_all_devs)
                disk_all_idle, disk_T, disk_has = _disk_source_state(_dt_eff)
            cpu_T = _fan_read_sys_temp("cpu") if need_cpu else None
            mb_T = _fan_read_sys_temp("mb") if need_mb else None
            for (hwmon, idx) in all_fans:
                rule = rules.get("%s::%d" % (hwmon, idx))
                if not rule:
                    continue
                src = rule.get("source", "disk")
                if src == "disk":
                    if not disk_has:
                        continue  # 未配置监控盘 → 该风扇本轮不温控（保持原样）
                    T, all_idle = disk_T, disk_all_idle
                else:
                    # cpu / mb / combo_max:cpu,mb / combo_avg:cpu,mb 统一经 _resolve_rule_temp 解析。
                    # 任一子源读数缺失则 T=None，由 _fan_rule_decision 保守交还自动（与旧逻辑一致）。
                    T, all_idle = _resolve_rule_temp(src, cpu_T, mb_T, disk_T, disk_all_idle, disk_has)
                key = (hwmon, idx)
                # #4 修复（huhaibo820）：用户显式手动调速（FAN_TARGETS 该风扇 mode=manual）
                # 时，手动优先于温度联动——否则一旦设了温度联控速，手动滑块就完全失效。
                # 点「恢复自动」会把 mode 改回 auto/清空，届时温度联动重新生效。
                ov = overrides.get(key)
                if ov and ov.get("mode") == "manual":
                    continue
                controlled.add(key)
                action, target = _fan_rule_decision(key, rule, T, all_idle=all_idle)
                if action == "control" and target is not None:
                    _fan_smooth_step(hwmon, idx, target); controlling_any = True
                elif action == "release":
                    _fan_release_auto(hwmon, idx)
            # "hold" → 已交还自动，不再写入（避免与主板/内核抢控）
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

def _fan_smooth_runner():
    """风扇控速常驻线程的『守护外壳』：fan_smooth_loop 内部虽已捕获 Exception，
    但为防止任何意外（含非 Exception 的 BaseException、SIGINT 等）导致控速线程静默死亡、
    进而使所有 PWM 变更（手动调速/温度联动）完全失效，此处再包一层自重启 + 异常落盘，
    确保控速线程永不永久退出，并把罕见崩溃原因写进 fan_thread.log 供排查。"""
    import traceback as _tb
    _log = os.path.join(_config_dir(), "fan_thread.log")
    while True:
        try:
            fan_smooth_loop()
        except BaseException as _e:   # 含 Exception 与 KeyboardInterrupt/SystemExit 等
            try:
                with open(_log, "a") as _fh:
                    _fh.write("%s [fan-smooth] 意外退出，2s 后自愈重启：%r\n%s\n"
                              % (time.strftime("%Y-%m-%d %H:%M:%S"), _e, _tb.format_exc()))
            except Exception:
                pass
            time.sleep(2)   # 退避后重启，避免空转打满 CPU

_restore_fan_modes()   # 启动即恢复风扇模式（或首次默认接管自动控温），避免重启后全速
_fan_thread = _threading.Thread(target=_fan_smooth_runner, daemon=True, name="fan-smooth")
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
        "idle_minutes": 5,           # 监控盘连续无读写满此分钟数也停转风扇（与休眠二选一满足即停，解决「设了休眠风扇却一直转」）
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
    """温度源单值（统一快照读取，~2s 刷新；控速线程/状态接口不再各自跑 sensors -j）。
    source='cpu' → CPU 封装温度（coretemp 核心 max / AMD Tdie，见 _parse_cpu_temp）；
    source='mb'  → 主板温度（SYSTIN 优先，见 _parse_mb_temp）。读不到返回 None。"""
    snap = _temp_snapshot_read()
    return snap.get("mb_temp") if (source or "cpu").lower() == "mb" else snap.get("cpu_temp")

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

def _disk_is_ssd(dev):
    """非 NVMe 的固态盘（SATA/SAS SSD）识别：/sys/block/<name>/queue/rotational=0。
    这类盘无机械轴、不会停转休眠，应排除出"全部休眠→停转"判断（否则像 SAS 一样
    把功能堵死）。NVMe 由调用方单独处理，这里只管 sdX 类设备。读 sysfs 失败则保守
    返回 False（按机械盘处理，宁可不排除也不误伤可休眠盘）。"""
    try:
        name = str(dev).replace("/dev/", "")
        with open("/sys/block/%s/queue/rotational" % name) as f:
            return f.read().strip() == "0"
    except Exception:
        return False


def get_disk_temps(devs):
    """读指定硬盘温度。sdX 用 smartctl -n standby（不唤醒休眠盘）；
    NVMe 不支持 -n standby，直接读温度（NVMe 一般不停机休眠）。
    返回 {dev: {"temp":int|None, "asleep":bool|None, "no_sleep":bool}}。
    no_sleep=True：该盘天生不会休眠——SAS 阵列企业盘被厂商为数据安全锁死不停转，
    NVMe 与 SATA/SAS 固态盘(SSD)无机械轴、也不停转休眠。这类盘不参与"全部休眠→停转"
    判断（否则一块 SAS/SSD 盘就永远满足不了 all(asleep)，把整个休眠停转功能堵死）。"""
    states = {}
    for dev in devs or []:
        # is_nvme 单独标记：NVMe 多为 M.2 被动散热（自带马甲、贴主板），机箱风扇气流基本吹不到，
        # 且其正常工作温区与机械盘完全不同（机械盘 44°C 偏热，NVMe 53°C 属正常、70°C 才近降频线）。
        # 故 NVMe 不能与机械盘共用 start_temp 阈值，需在停转兜底与温度源取值处单独换算。
        is_nvme = dev.startswith("/dev/nvme")
        try:
            no_sleep = False
            if is_nvme:
                out = sudo_cmd([SMARTCTL, "-A", dev], 8)
                asleep = False
                no_sleep = True  # NVMe 不停机休眠
            else:
                out = sudo_cmd([SMARTCTL, "-n", "standby", "-A", dev], 8)
                asleep = False
                if out and "STANDBY" in out.upper():
                    states[dev] = {"dev": dev, "temp": None, "asleep": True,
                                   "no_sleep": False, "is_nvme": False}
                    continue
                # SAS 企业盘（阵列卡后）永不休眠：厂商为数据安全锁死。
                # smartctl -A 对 SAS 盘输出 "Current Drive Temperature"（SATA 走属性表
                # Temperature_Celsius），据此识别，无需额外调用；阵列卡后 lsblk TRAN 为空不可用。
                if out:
                    up = out.upper()
                    if ("CURRENT DRIVE TEMPERATURE" in up
                            or "DRIVE TRIP TEMPERATURE" in up
                            or "TRANSPORT PROTOCOL:   SAS" in up):
                        no_sleep = True
                # SATA/SAS 固态盘(SSD)无机械轴、不会停转休眠，同样排除出判断
                if not no_sleep and _disk_is_ssd(dev):
                    no_sleep = True
            if not out:
                states[dev] = {"dev": dev, "temp": None, "asleep": None,
                               "no_sleep": no_sleep, "is_nvme": is_nvme}
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
            states[dev] = {"dev": dev, "temp": temp, "asleep": asleep,
                           "no_sleep": no_sleep, "is_nvme": is_nvme}
        except Exception:
            states[dev] = {"dev": dev, "temp": None, "asleep": None,
                           "no_sleep": False, "is_nvme": is_nvme}
    return states


# 磁盘温度短缓存：/api/fan/temps 被前端每 5s 轮询、风扇控速线程每 0.6s tick、
# /api/fan/status 每 1s 轮询都会读盘温，而 get_disk_temps 对每块盘跑 smartctl
# （每块 8s 超时），多盘时单次可能耗时数秒~数十秒（盘休眠/慢盘时尤甚）。
# 无缓存时这些高频调用会叠起海量慢扫描（实测曾每秒十几发 smartctl 打满磁盘 I/O），
# 把后端与网关拖死（表现为整个面板所有接口全部卡住、风扇转速冻结、温度卡片空转）。
# 因此统一走本缓存：TTL 4s 合并所有调用为同一次扫描；扫描进行中时调用方直接返回
# 旧快照（宁可旧几秒，绝不阻塞）。前端另有「沿用上次已知温度」兜底，数值不会消失。
_DISK_TEMP_CACHE = {"t": 0.0, "v": None, "devs": None}
_DISK_TEMP_TTL = 4.0
_DISK_TEMP_LOCK = _threading.Lock()

def get_disk_temps_cached(devs):
    now = time.time()
    key = tuple(devs or ())
    c = _DISK_TEMP_CACHE
    if c["devs"] == key and now - c["t"] <= _DISK_TEMP_TTL:
        return c["v"]
    # 缓存过期：尝试获取扫描权（非阻塞）；拿不到说明另一线程正在扫，直接返回旧值。
    if _DISK_TEMP_LOCK.acquire(blocking=False):
        try:
            # 双重检查：等待期间可能已被其他线程刷新
            if c["devs"] != key or now - c["t"] > _DISK_TEMP_TTL:
                c["v"] = get_disk_temps(devs or [])
                c["devs"] = key
                c["t"] = time.time()
        finally:
            _DISK_TEMP_LOCK.release()
        return c["v"]
    return c["v"]


_DISK_IO_CACHE = {}   # dev -> {"sectors": int, "t": float}；按 /proc/diskstats 扇区计数判断磁盘是否真的在读写
_DISK_FRIENDLY_CACHE = {}   # dev -> {"name","model","category","is_system","size","t"}; 5 分钟缓存
_DISK_FRIENDLY_TTL = 300
_SYSTEM_DISK_DEV = None   # 启动时确定一次（系统盘一般不会换）


def _detect_system_disk_dev():
    """确定 / 挂载的盘 dev（如 sda/nvme0n1）。挂载在 /boot、/boot/efi 不算系统盘。"""
    global _SYSTEM_DISK_DEV
    if _SYSTEM_DISK_DEV is not None:
        return _SYSTEM_DISK_DEV
    try:
        out = run_cmd(["findmnt", "-n", "-o", "SOURCE", "/"], 3)
        src = (out.strip().splitlines() or [""])[0].strip()
        # 形如 /dev/nvme0n1p2[/subvol] 的 btrfs 源，先砍掉子卷部分
        m0 = re.match(r"^(/dev/[A-Za-z0-9/_.-]+?)(?:\[.*\])?$", src)
        if m0:
            src = m0.group(1)
        part = src.replace("/dev/", "").strip()
        if part:
            # 首选 lsblk PKNAME 直接问父盘（对 nvme/mmcblk/dm 都稳）
            try:
                pk = run_cmd(["lsblk", "-no", "PKNAME", src], 3).strip().splitlines()
                pk0 = pk[0].strip() if pk else ""
                if pk0:
                    _SYSTEM_DISK_DEV = pk0
                    return _SYSTEM_DISK_DEV
            except Exception:
                pass
            # 回退：手工剥离分区后缀（nvme0n1p2 → nvme0n1；mmcblk0p1 → mmcblk0；sda3 → sda）
            mn = re.match(r"^(nvme\d+n\d+)p\d+$", part) or re.match(r"^(mmcblk\d+)p\d+$", part)
            if mn:
                _SYSTEM_DISK_DEV = mn.group(1)
            else:
                _SYSTEM_DISK_DEV = re.sub(r"\d+$", "", part)
    except Exception:
        pass
    return _SYSTEM_DISK_DEV or ""


def _short_size(size_b):
    """字节数 → '14.0T'/'120G'。"""
    try:
        b = int(size_b)
    except Exception:
        return "?"
    gb = b / 1e9
    if gb >= 1000:
        v = gb / 1000
        return f"{v:.0f}T" if v >= 10 else f"{v:.1f}T"
    return f"{gb:.0f}G"


def _short_disk_name(model, size_str, category, is_system):
    """生成盘的简短可读名（用于「当前温度」卡片）。"""
    if is_system:
        return f"系统盘({category})"
    # 取品牌简称（去掉 "（xxx）" 后缀）
    brand_cn = ""
    if model:
        b, _ = disk_brand_and_feature(model)
        if b:
            brand_cn = b.split("(")[0]
    if brand_cn and size_str and size_str != "?":
        return f"{brand_cn}{size_str}"
    if model:
        return model[:16]
    return "未知"


def _disk_friendly_info(dev):
    """返回该盘的友好名/型号/分类/盘位/是否系统盘。带 5 分钟缓存。
    设计目标：让前端「当前温度」卡片上的 nvme0n1/sda/sdb 一眼能认出是哪块物理盘。"""
    dev = str(dev).replace("/dev/", "").strip()
    if not dev:
        return {"dev": "", "name": dev, "model": "", "category": "HDD", "is_system": False}
    now = time.time()
    cached = _DISK_FRIENDLY_CACHE.get(dev)
    if cached and now - cached["t"] < _DISK_FRIENDLY_TTL:
        return cached
    info = {"dev": dev, "name": dev, "model": "", "category": "HDD",
            "is_system": False, "size": "?", "t": now}
    # 1) lsblk 轻量拿 ROTA/TRAN/SIZE
    try:
        lsblk = run_cmd(["lsblk", "-dn", "-b", "-o", "NAME,SIZE,ROTA,TRAN", f"/dev/{dev}"], 3)
        for line in lsblk.strip().splitlines():
            p = line.split()
            if p and p[0] == dev and len(p) >= 2:
                info["size"] = _short_size(p[1])
                rota = p[2] if len(p) > 2 else "1"
                tran = p[3] if len(p) > 3 else ""
                if dev.startswith("nvme"):
                    info["category"] = "NVMe"
                elif rota == "0":
                    info["category"] = "SSD"
                else:
                    info["category"] = "HDD"
                break
    except Exception:
        pass
    # 2) smartctl -i 拿 model（带 -n standby 不唤醒休眠盘）
    try:
        out = sudo_cmd([SMARTCTL, "-n", "standby", "-i", f"/dev/{dev}"], 5)
        if out:
            m = re.search(r"(?:Device Model|Model Number|Product):\s*(.+)", out)
            if m:
                info["model"] = m.group(1).strip()
    except Exception:
        pass
    # 3) 系统盘判定
    sys_dev = _detect_system_disk_dev()
    info["is_system"] = (dev == sys_dev)
    # 4) 友好名
    info["name"] = _short_disk_name(info["model"], info["size"], info["category"], info["is_system"])
    _DISK_FRIENDLY_CACHE[dev] = info
    return info

def _list_all_disk_devs():
    """返回本机全部块设备路径（/dev/sdX + /dev/nvmeXnY），供「逐风扇 disk 规则」在用户未配置
    全局监控盘白名单（fan_disk_temp.disks 为空）时，自动用全部盘作为温度源。
    避免用户只设了逐风扇 disk 规则、却因全局 disks 为空导致风扇被静默跳过、永远不控。
    轻量 glob 枚举，不触发 smartctl（温度在调速线程里按需读取）。"""
    names = set()
    for l in "\n".join(_glob.glob("/dev/sd*")).split():
        n = l.strip().split("/")[-1]
        if re.match(r"^sd[a-z]+$", n):
            names.add(n)
    for l in "\n".join(_glob.glob("/dev/nvme*")).split():
        n = l.strip().split("/")[-1]
        if re.match(r"^nvme\d+n\d+$", n):
            names.add(n)
    return ["/dev/" + n for n in sorted(names)]


def _disk_idle(dev, idle_minutes):
    """按 /proc/diskstats 的扇区读写计数判断磁盘是否已连续 idle 满 idle_minutes。
    与 smartctl 检测到的 STANDBY 状态互补：部分环境下 STANDBY 检测不到，但盘确实无 I/O，
    此时也应允许停转风扇（用户「设了 5 分钟休眠风扇却一直转」多因此而来）。
    返回 True=已连续无 I/O 足够久（可停转）；False=近期有 I/O 或尚在计时窗口内。"""
    try:
        name = str(dev).replace("/dev/", "")
        last = None
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                # 字段：1 major 2 minor 3 dev 4 reads 5 rmerged 6 rsect 7 rms 8 writes 9 wmerged 10 wsect
                if len(parts) >= 10 and parts[2] == name:
                    last = int(parts[5]) + int(parts[9])
                    break
        now = time.time()
        if last is None:
            return False
        prev = _DISK_IO_CACHE.get(dev)
        if prev is None or prev["sectors"] != last:
            _DISK_IO_CACHE[dev] = {"sectors": last, "t": now}
            return False
        return (now - prev["t"]) >= idle_minutes * 60
    except Exception:
        return False

# NVMe 专用温度量纲。机械盘与 NVMe 的"正常/该散热"温区完全不同：
#   机械盘 40°C 已偏热、50°C 需警惕；NVMe 53°C 属完全正常，70~75°C 才触发厂商降频保护。
# 早期两者共用 start_temp(默认 40°C)，导致任何一块 NVMe 都恒高于阈值，
# 于是"硬盘全部休眠即停转"被永久判定为 False——用户勾了停转却发现风扇一直转。
# 另一面也要留安全阀：装了「主动式 M.2 散热器」（带小风扇、接主板风扇口）的机器，
# NVMe 真烧起来时风扇仍需响应，故不是直接排除，而是换算到 NVMe 自己的量纲。
NVME_START_TEMP = 65.0   # NVMe 开始需要主动散热的温度
NVME_FULL_TEMP = 80.0    # NVMe 需全力散热的温度（接近多数消费级盘的降频/告警线）

def _nvme_temp_as_hdd(t, start, full):
    """把 NVMe 温度换算成等效的机械盘温度，便于与其他盘统一比较/取最大值。
    低于 NVME_START_TEMP 返回 None（不参与，视为无需风扇介入）；
    NVME_START→start、NVME_FULL→full 线性映射，使风扇响应强度与"离降频线还有多远"对齐。"""
    if not isinstance(t, (int, float)) or t < NVME_START_TEMP:
        return None
    if t >= NVME_FULL_TEMP:
        return full
    r = (t - NVME_START_TEMP) / (NVME_FULL_TEMP - NVME_START_TEMP)
    return start + r * (full - start)

def _all_monitored_idle(valid, cfg):
    """判断"可停转风扇"：所有可休眠盘都已休眠(或连续无 I/O 满 idle_minutes)，且不可休眠盘
    (SAS 阵列企业盘/NVMe/SATA·SAS 固态盘)温度都低于启动阈值。SAS 企业盘与固态盘厂商锁死/
    无机械轴不休眠，不参与"全部休眠"判断，但它们若高温仍需散热(安全兜底)，此时不停转、
    交给温度曲线控速。
    - sleep_stop 关 → 永不停转
    - 无任何可休眠盘(如纯 SAS/NVMe/SSD 环境) → 永不因休眠停转，交给温度曲线
    - 任一可休眠盘未休眠且仍有 I/O（未满 idle_minutes）→ 不停转
    """
    if not bool(cfg.get("sleep_stop", True)):
        return False
    idle_min = float(cfg.get("idle_minutes", 5))
    sleepable = [s for s in valid if not s.get("no_sleep")]
    if not sleepable:
        return False
    for s in sleepable:
        if s.get("asleep"):
            continue
        if _disk_idle(s.get("dev"), idle_min):
            continue
        return False  # 该盘既未休眠、也未空闲够久 → 不停转
    start = float(cfg.get("start_temp", 40))
    for s in valid:
        if not s.get("no_sleep"):
            continue
        t = s.get("temp")
        if not isinstance(t, (int, float)):
            continue
        # NVMe 按自己的量纲判定（65°C 起）：机械盘的 40°C 对 NVMe 只是常温，
        # 若共用会让本函数恒返回 False，等于永久禁用「休眠停转」。
        thr = NVME_START_TEMP if s.get("is_nvme") else start
        if t >= thr:
            return False  # 有不休眠的盘确实偏热（SAS 企业盘/固态），仍需散热，不停转
    return True

def _disk_source_max_temp(valid, start, full):
    """取监控盘的「等效最热温度」，作为硬盘温度源的单值 T。
    机械盘 / SATA·SAS 固态用原始温度；NVMe 先换算到机械盘量纲（低于 65°C 直接不参与）。
    否则常年 50°C 出头的 M.2 固态会稳坐"最热盘"，让机箱风扇的转速由一块它根本吹不到的盘决定，
    真正需要散热的机械盘反而说了不算。"""
    temps = []
    for s in valid:
        t = s.get("temp")
        if not isinstance(t, (int, float)):
            continue
        if s.get("is_nvme"):
            t = _nvme_temp_as_hdd(t, start, full)
            if t is None:
                continue
        temps.append(t)
    return max(temps) if temps else None

def _fan_disk_temp_pwm(states, cfg):
    """按硬盘温度算目标 raw(0~255)。
    - 所有监控盘休眠且 sleep_stop → 0（停转）
    - 优先自定义温度→PWM 曲线；否则取最热盘温度 T：T<start → 0；start≤T<full → min~max 线性；T≥full → max
    """
    valid = [s for s in (states or {}).values() if isinstance(s, dict)]
    if not valid:
        return None
    if _all_monitored_idle(valid, cfg):
        return 0
    start = float(cfg.get("start_temp", 40))
    full = float(cfg.get("full_temp", 60))
    minp = float(cfg.get("min_pwm", 30))
    maxp = float(cfg.get("max_pwm", 70))
    T = _disk_source_max_temp(valid, start, full)
    if T is None:
        # 区分两种「没有可用温度」：
        #   完全读不到 → 保守兜底转起来（怕真热却不知道）；
        #   读得到但都低于各自开转阈值（例如只监控了一块 55°C 的 NVMe）→ 无需风扇，给 0。
        if not any(isinstance(s.get("temp"), (int, float)) for s in valid):
            if cfg.get("curve"):
                return _fan_curve_pwm(None, cfg, 30, 70)
            return round(float(cfg.get("min_pwm", 30)) / 100 * 255)
        return 0
    curve_raw = _fan_curve_pwm(T, cfg, 30, 70)
    if curve_raw is not None:
        return curve_raw
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

# ---- 风扇接口调速信号类型：PWM(4pin 脉宽) / DC(3pin 直流电压) ----
# sysfs 语义（hwmon 标准）：pwmN_mode  0=DC(电压调速)  1=PWM(脉宽调速)
# 实测(NCT6797/微星 B360M MORTAR)：文件对所有通道都存在且 0644 可写，但只有部分通道
# 真正支持切到 DC——不支持的通道写 0 会被内核直接拒绝(OSError EIO/EINVAL)，值保持 1。
# 因此「是否支持 DC」无法在不改动硬件状态的前提下探测（驱动对写 1 一律接受、写 0 才校验），
# 只在用户显式切换时尝试写入，失败则返回明确原因，不做启动期试探。
def _fan_read_pwm_mode(hwmon, idx):
    """读取该风扇接口的调速信号类型。返回 'pwm' / 'dc' / None(该通道无此文件)。"""
    try:
        with open(f"{hwmon}/pwm{idx}_mode") as f:
            v = f.read().strip()
        return "pwm" if v == "1" else "dc" if v == "0" else None
    except Exception:
        return None

def _fan_set_pwm_mode(hwmon, idx, mode):
    """切换调速信号类型。mode: 'pwm' | 'dc'。返回 (ok, err_msg)。
    注意：DC 模式下占空比是按电压比例输出，低占空比更容易导致风扇停转或无法启动。"""
    val = "1" if mode == "pwm" else "0"
    path = f"{hwmon}/pwm{idx}_mode"
    if not os.path.exists(path):
        return False, "该接口不支持切换调速方式（主板未提供此寄存器）"
    try:
        with open(path, "w") as f:
            f.write(val)
    except OSError as e:
        # EIO/EINVAL = 主板该接口硬件上只支持一种信号类型，无法切换
        if e.errno in (errno.EIO, errno.EINVAL):
            return False, "该接口硬件只支持 %s 模式，无法切换（主板限制，非软件问题）" % (
                "PWM" if mode == "dc" else "DC")
        return False, "写入失败：%s" % e
    except Exception as e:
        return False, "写入失败：%s" % e
    # 回读确认（部分驱动写入不报错但也不生效）
    if _fan_read_pwm_mode(hwmon, idx) != mode:
        return False, "该接口硬件不支持 %s 模式（写入未生效）" % mode.upper()
    return True, None

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
    if _all_monitored_idle(valid, cfg):
        _dt_engaged["v"] = False
        return ("release", None)
    # NVMe 换算后再取最热：否则 M.2 固态常年 50°C+ 会让滞回状态恒定判为"该接管"，
    # 风扇永远交不回主板自动控制（与停转失效同源）。
    T = _disk_source_max_temp(valid, start, float(cfg.get("full_temp", 60)))
    if T is None:
        _dt_engaged["v"] = False
        return ("release", None)
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

# ===================== 风扇：逐风扇温度联动规则（fan_rules）=====================
# 论坛需求（huhaibo820）：两台硬盘风扇跟同一组硬盘温度走，但各用不同曲线
# （一路 30–45°C、一路 45–60°C）。即「在风扇处设置温度源」——每台风扇可单独选温度源
# （硬盘聚合 / CPU / 主板）并配自己的曲线。
# 设计（向后兼容，叠加式）：
#   - 默认规则由旧的 disk_temp / sys_temp 两个全局面板派生（谁 enabled + 接管哪些风扇）。
#   - fan_rules.json 存「逐风扇覆盖」：某台风扇一旦单独设置，即以它为准，压过全局默认。
#   - 硬盘温度源共享 disk_temp 的「监控硬盘 + 休眠/空闲停转」设置（同阵列同温源）。
FAN_RULES_FILE = os.path.join(_config_dir(), "fan_rules.json")
_FAN_RULE_SOURCES = ("disk", "cpu", "mb")

def _load_fan_rules_raw():
    """读取用户保存的逐风扇覆盖规则；无有效文件返回 None（表示未自定义、沿用全局默认）。"""
    d = _load_json_file(FAN_RULES_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("rules"), dict):
        return d
    return None

def _save_fan_rules(d):
    return _save_json_file(FAN_RULES_FILE, d)

def _rule_from_cfg(source, cfg):
    """把全局 disk_temp / sys_temp 配置转成一条逐风扇规则。"""
    r = {
        "enabled": True,
        "source": source,
        "start_temp": cfg.get("start_temp", 40),
        "full_temp": cfg.get("full_temp", 60),
        "min_pwm": cfg.get("min_pwm", 30),
        "max_pwm": cfg.get("max_pwm", 100),
        "recover_temp": cfg.get("recover_temp", 35),
    }
    if cfg.get("curve"):
        r["curve"] = cfg["curve"]
    return r

def _derive_rules_from_legacy():
    """从旧的 disk_temp / sys_temp 全局面板派生逐风扇默认规则（sys 优先于 disk）。"""
    st = _load_fan_sys_temp()
    dt = _load_fan_disk_temp()
    all_fans = _enumerate_fans()
    sys_claimed, disk_claimed = _select_temp_fans(all_fans, st, dt)
    rules = {}
    for (h, i) in sys_claimed:
        rules["%s::%d" % (h, i)] = _rule_from_cfg(st.get("source", "cpu"), st)
    for (h, i) in disk_claimed:
        rules["%s::%d" % (h, i)] = _rule_from_cfg("disk", dt)
    return rules

def _effective_fan_rules():
    """最终生效的逐风扇规则：先由旧全局面板派生默认，再叠加用户逐风扇覆盖。
    覆盖项 value=None 表示「显式清除该风扇的规则」（回到手动/BIOS）。"""
    rules = _derive_rules_from_legacy()
    saved = _load_fan_rules_raw()
    if saved:
        for k, r in (saved.get("rules") or {}).items():
            if r is None:
                rules.pop(k, None)
            elif isinstance(r, dict) and r.get("enabled", True):
                rules[k] = r
            else:
                rules.pop(k, None)  # enabled=False → 该风扇不温控
    # hwmon 路径漂移兜底：飞牛重启后 /sys/class/hwmon/hwmonN 编号会变化
    # (如 hwmon4<->hwmon3)，已保存规则的 key 用的是旧路径，精确匹配会整组失效、
    # 表现为「部分风扇温控丢失 / 只控一组」。按通道序号(idx)兜底：当前枚举到的
    # (hwmon,idx) 若精确 key 缺失、但存在同 idx 的旧 key，则映射到当前精确 key。
    # 与 fan_labels 的 idx 兜底策略一致。
    try:
        _enum = _enumerate_fans()
        _fixed = {}
        for (_hw, _idx) in _enum:
            _ek = "%s::%d" % (_hw, _idx)
            if _ek in rules:
                continue
            _suf = "::%d" % _idx
            for _k, _r in rules.items():
                if _k.endswith(_suf):
                    _fixed[_ek] = _r
                    break
        rules.update(_fixed)
    except Exception:
        pass
    return rules

def _disk_source_state(dt_cfg):
    """硬盘温度源当前状态（供所有 source=disk 的风扇共享，每轮只算一次）。
    返回 (all_idle, T, has_disks)：
      all_idle=True → 监控盘全部休眠/空闲 → 交还自动
      T            → 最热监控盘温度（°C），无读数为 None
      has_disks    → 是否配置了监控盘（无则 disk 源风扇不参与温控）"""
    devs = dt_cfg.get("disks") or []
    if not devs:
        return (False, None, False)
    states = get_disk_temps_cached(devs)
    valid = [s for s in states.values() if isinstance(s, dict)]
    if not valid:
        return (False, None, True)
    if _all_monitored_idle(valid, dt_cfg):
        return (True, None, True)
    # NVMe 先换算到机械盘量纲再参与取最热（低于 65°C 不参与），
    # 否则常年 50°C 出头的 M.2 固态会恒定当选"最热盘"，把机箱风扇一直顶在中高速。
    T = _disk_source_max_temp(valid,
                              float(dt_cfg.get("start_temp", 40)),
                              float(dt_cfg.get("full_temp", 60)))
    return (False, T, True)

def _fan_rule_pwm(T, rule):
    """按单值温度 T + 该风扇自身规则算目标 raw(0~255)。
    active_mode 控制走哪套（3按钮显式选择，根治「不知道当前跑的是哪套」）：
      'curve'  → 自定义曲线；曲线无效/缺失时回退到线性（不报错）
      'linear' → 开转/全速 线性；忽略自定义曲线（即便设了也走线性）
      未设/None → 旧行为：曲线优先，线性兜底（兼容老配置）"""
    minp = float(rule.get("min_pwm", 30))
    maxp = float(rule.get("max_pwm", 100))
    active_mode = rule.get("active_mode")
    if active_mode == "curve":
        curve_raw = _fan_curve_pwm(T, rule, minp, maxp)
        if curve_raw is not None:
            return curve_raw
        # 曲线无效/缺失：回退线性（不写错，避免显式选曲线但无曲线时 0 输出）
    elif active_mode == "linear":
        pass  # 直接走线性
    else:
        # 未设/旧配置：曲线优先（保持向后兼容）
        curve_raw = _fan_curve_pwm(T, rule, minp, maxp)
        if curve_raw is not None:
            return curve_raw
    if T is None:
        return None
    start = float(rule.get("start_temp", 40))
    full = float(rule.get("full_temp", 60))
    if T < start:
        return 0
    if T >= full:
        return round(maxp / 100 * 255)
    r = (T - start) / (full - start) if full > start else 0
    return round((minp + r * (maxp - minp)) / 100 * 255)

# 逐风扇温控滞回状态：{(hwmon, idx): True/False/None}
_FAN_ENGAGED = {}

def _fan_rule_decision(key, rule, T, all_idle=False):
    """逐风扇温控滞回状态机，语义与旧的两套全局状态机一致但按风扇独立记忆。
    返回 (action, raw)：control=按曲线接管 / release=交还自动 / hold=已释放且在滞回区不写。
    stop_below_start=True 时：温度低于开转温度（或硬盘空闲=冷态）直接强制 0%（保持在 nasdash
    软件接管、不交还自动），实现「低于开转温度即停转」。读不到温度仍保守交还自动（避免未知
    高温时风扇熄火，比强制停转更安全）。"""
    start = float(rule.get("start_temp", 40))
    recover = float(rule.get("recover_temp", start - 5))
    if recover >= start:
        recover = start - 5
    stop_below = bool(rule.get("stop_below_start", False))
    if all_idle:
        # 硬盘空闲（disk 源）视为冷态：勾选了低温停转则强制 0%，否则交还自动
        _FAN_ENGAGED[key] = False
        return ("control", 0) if stop_below else ("release", None)
    if T is None:
        # 读不到温度：保守交还自动（不强制停转，避免未知高温时风扇熄火）
        _FAN_ENGAGED[key] = False
        return ("release", None)
    if T < start:
        if stop_below:
            # 低温强制停转：直接写 0%（保持在 nasdash 软件接管，不交还自动）
            _FAN_ENGAGED[key] = False
            return ("control", 0)
        # 原滞回逻辑：已接管则低于 recover 才释放；未接管则保持释放
        if _FAN_ENGAGED.get(key):
            if T < recover:
                _FAN_ENGAGED[key] = False
                return ("release", None)
            return ("control", _fan_rule_pwm(T, rule))
        return ("hold", None)
    # 已达开转温度：接管并按曲线控速
    _FAN_ENGAGED[key] = True
    return ("control", _fan_rule_pwm(T, rule))

def _resolve_rule_temp(src, cpu_T, mb_T, disk_T, disk_all_idle, disk_has):
    """按温度源解析出 (T, all_idle)，供 _fan_rule_decision 使用。
    src 支持：
      disk                → 硬盘温度（disk_has 为假时返回 (None, False) 表示本轮跳过）
      cpu / mb            → CPU / 主板温度
      combo_max:cpu,mb    → 取 CPU 与主板温度的【较大值】
      combo_avg:cpu,mb    → 取 CPU 与主板温度的【平均值】
    子项可递归（combo 里还能套 sensor: 等）。任一子源不可用时忽略该子源；
    全部不可用时返回 (None, False)。移植自 guan-ry/FanControlServerApp 的 resolveSourceTemp。
    彻底解决「选了主板 CPU 就没了」的单选互斥痛点（论坛 huhaibo820 反馈）。"""
    src = (src or "disk").strip()
    if src == "disk":
        return (disk_T if disk_has else None, disk_all_idle)
    if src == "cpu":
        return (cpu_T, False)
    if src == "mb":
        return (mb_T, False)
    if src.startswith("combo_avg:") or src.startswith("combo_max:"):
        parts = [s.strip() for s in src.split(":", 1)[1].split(",") if s.strip()]
        vals = []
        for p in parts:
            if p == "cpu":
                vals.append(cpu_T)
            elif p == "mb":
                vals.append(mb_T)
            elif p == "disk":
                vals.append(disk_T if disk_has else None)
            else:
                t, _ = _resolve_rule_temp(p, cpu_T, mb_T, disk_T, disk_all_idle, disk_has)
                vals.append(t)
        vals = [v for v in vals if isinstance(v, (int, float))]
        if not vals:
            return (None, False)
        if src.startswith("combo_max"):
            return (max(vals), False)
        return (sum(vals) / len(vals), False)
    return (None, False)

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

@_ttl_cache(600)
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
            out = sudo_cmd(["smartctl", "-n", "standby", "-i", "/dev/" + d], 8)
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


def _parse_vds_from_topology(out):
    """从 storcli /c0 show 的 TOPOLOGY 表解析 Virtual Drive（逻辑盘）元信息。"""
    vds = []
    in_topo = False
    for line in out.splitlines():
        s = line.strip()
        if 'DG/VD' in s and 'TYPE' in s and 'State' in s:
            in_topo = True
            continue
        if not in_topo:
            continue
        if re.match(r'^-+$', s):
            in_topo = False
            continue
        if not s:
            continue
        parts = s.split()
        if len(parts) < 6 or not re.match(r'^\d+/\d+$', parts[0]):
            continue
        dgvd, vtype, state, access = parts[0], parts[1], parts[2], parts[3]
        consist, cache_code = parts[4], parts[5]
        cac = parts[6] if len(parts) > 6 else ''
        scc = parts[7] if len(parts) > 7 else ''
        rest = parts[8:]
        size, name = '', ''
        if rest:
            if re.match(r'^[\d.]+$', rest[0]):
                unit = rest[1] if len(rest) > 1 and re.match(r'^[TGMK]B?$', rest[1]) else ''
                size = rest[0] + ((' ' + unit) if unit else '')
                name = ' '.join(rest[2:]) if len(rest) > 2 else ''
            else:
                name = ' '.join(rest)
        vds.append({
            "dgvd": dgvd, "type": vtype, "state": state, "access": access,
            "consist": consist, "cache_code": cache_code, "cac": cac, "scc": scc,
            "size": size, "name": name,
            "write_policy": "", "read_policy": "", "read_cache": "", "wb": None, "cache_raw": ""
        })
    return vds


def _parse_vd_cache_policies(vd_out):
    """从 storcli /c0/vall show 按 DG/VD 块解析每个 VD 的 Cache Policy 文本。"""
    res = {}
    if not vd_out:
        return res
    anchors = [(m.start(), m.group(1)) for m in re.finditer(r"DG/VD:\s*(\d+/\d+)", vd_out)]
    if not anchors:
        anchors = [(m.start(), m.group(1)) for m in re.finditer(r"Virtual Drive:\s*(\d+)", vd_out)]
    for i, (pos, key) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(vd_out)
        block = vd_out[pos:end]
        cp = re.search(r"Default Cache Policy\s*:\s*(.+)", block)
        res[key] = cp.group(1).strip() if cp else ""
    return res


@_ttl_cache(3600)
def _probe_locate_support(rc_raw=""):
    """只读判断阵列卡/背板是否支持物理盘定位（locate LED 闪烁）。

    关键修正（v2.0.8）：早期版本误把 enclosure 行里出现的 "SGPIO" 字样当作
    「背板支持定位」。但 SGPIO/SES 只是控制器/expander 固件声明的**协议能力**，
    不代表机箱背板的 LED 真的接了定位信号线。实测 NAS-3 V2.5 被动背板：
    enclosure 行 SIM=1、ProdID 占位为 "SGPIO"，storcli start locate 返回
    Succeeded，但机箱盘位灯实际不闪（背板仅接 SATA 活动 LED）。

    因此本函数收紧判据：
      - 必须有 SES 带内管理接口（SIM 列 == 1）
      - 且背板上报了**真实型号**（ProdID 非空、且不是 SGPIO/SES 这类协议占位名）
    二者同时满足才认为「用户点了真能看见灯闪」，否则隐藏定位按钮，
    避免给用户一个「点了没反应」的假功能。

    本函数**绝不触发真实闪灯**。
    """
    if not rc_raw:
        try:
            rc_raw = sudo_cmd([STORCLI, "/c0", "show"], 30) or ""
        except Exception:
            return False
    if not rc_raw:
        return False
    # 解析 Enclosure LIST 块：列序 EID State Slots PD PS Fans TSs Alms SIM Port# ProdID VendorSpecific
    lines = rc_raw.splitlines()
    in_list = False
    for ln in lines:
        s = ln.strip()
        if "Enclosure LIST" in ln:
            in_list = True
            continue
        if not in_list:
            continue
        if not s:
            break  # Enclosure 块结束
        if s.startswith("EID") or s.startswith("---"):
            continue
        if not s[0].isdigit():
            continue
        parts = ln.split()
        if len(parts) < 10:
            continue
        try:
            sim = parts[8]
            prod = parts[10] if len(parts) > 10 else ""
        except Exception:
            continue
        # 需要真有 SES 管理接口，且背板上报了真实型号（非 SGPIO/SES 这类协议占位）
        if sim == "1" and prod and prod not in ("-", "SGPIO", "SES", "SES2", "VendorSpecific"):
            return True
    return False


@_ttl_cache(60)
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
        # 热备盘（全局 GHS / 专用 DHS）
        hot = []
        for d in drives:
            st = (d.get("state") or "").upper()
            if "GHS" in st:
                d2 = dict(d); d2["hs_type"] = "global"; hot.append(d2)
            elif "DHS" in st:
                d2 = dict(d); d2["hs_type"] = "dedicated"; hot.append(d2)
        data["hotspares"] = hot
        # ---- CopyBack 自动换盘状态 ----
        # Auto CopyBack 是阵列卡固件能力：某盘故障且已配热备盘时，自动把数据复制到热备盘完成换盘。
        # 面板负责「监控状态 + 开关自动 CopyBack + 手动触发」，真正换盘由阵列卡固件执行。
        cb_out = sudo_cmd([STORCLI, "/c0", "show", "copyback"], 10)
        if cb_out and "Auto CopyBack" in cb_out:
            m = re.search(r"Auto\s+CopyBack\s*:\s*(\w+)", cb_out)
            data["auto_copyback"] = m.group(1).strip().lower() if m else "unknown"
        else:
            data["auto_copyback"] = "unknown"
        for d in drives:
            st = (d.get("state") or "").upper()
            d["copyback_active"] = "COPYBACK" in st
            d["failed"] = ("FAILED" in st) or ("RBAD" in st)
        # 待 CopyBack 换盘的盘：已故障 + 存在热备盘 + 已开启自动 CopyBack
        data["copyback_needed"] = [
            {"slot": d["slot"], "model": d.get("model", "")}
            for d in drives
            if d.get("failed") and data["hotspares"] and data["auto_copyback"] == "enabled"
        ]
        # ---- Virtual Drives（逻辑盘）+ 缓存策略 ----
        try:
            vds = _parse_vds_from_topology(out)
            if vds:
                vd_out = sudo_cmd([STORCLI, "/c0", "/vall", "show"], 30)
                cp_map = _parse_vd_cache_policies(vd_out)
                for v in vds:
                    raw = cp_map.get(v["dgvd"], "")
                    up = raw.upper()
                    if up:
                        v["cache_raw"] = raw
                        v["write_policy"] = "WriteBack" if "WRITEBACK" in up else ("WriteThrough" if "WRITETHROUGH" in up else "")
                        v["read_policy"] = "NoReadAhead" if "NOREADAHEAD" in up else ("ReadAhead" if "READAHEAD" in up else "")
                        v["read_cache"] = "Cached" if "CACHED" in up else ("Direct" if "DIRECT" in up else "")
                        v["wb"] = (v["write_policy"] == "WriteBack")
                    else:
                        code = (v.get("cache_code") or "").upper()
                        v["cache_raw"] = code
                        v["write_policy"] = "WriteBack" if len(code) > 1 and code[1] == "W" else ("WriteThrough" if len(code) > 1 and code[1] == "T" else "")
                        v["wb"] = (v["write_policy"] == "WriteBack")
            data["virtual_drives"] = vds
        except Exception as e:
            data["virtual_drives"] = []
            _debug("parse VD cache failed: " + str(e))
        data["locate_supported"] = _probe_locate_support(data.get("raw", ""))
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

# ===================== 硬盘自检（A/B/C 三档）=====================
# A 档：smartctl -t long（只读， firmware 面扫，安全，可后台运行）。
# B 档：badblocks -wsv（读写覆盖，破坏性，仅允许独立盘：未挂载、非 RAID/LVM 成员）。
# C 档：badblocks -sv（只读表面扫描，不写数据、不伤盘，可在用盘/阵列成员上运行，1 遍读）。
# B/C 档都会把坏块 LBA 实时/结束时写入 -o 文件，供前端画「哨兵式」扇区网格。
DISK_TEST_LOCK = _threading.Lock()
DISK_TEST_JOBS = {}
# 自检历史持久化：每次自检完成/中止/失败追加一条，保留最近 50 条（供「自检记录」回查）
DISK_TEST_HISTORY_FILE = os.path.join(_config_dir(), "disk_test_history.json")
DISK_TEST_HISTORY_MAX = 50


def _load_disk_test_history():
    h = _load_json_file(DISK_TEST_HISTORY_FILE, [])
    return h if isinstance(h, list) else []


def _append_disk_test_history(record):
    try:
        hist = _load_disk_test_history()
        hist.append(record)
        hist = hist[-DISK_TEST_HISTORY_MAX:]
        _save_json_file(DISK_TEST_HISTORY_FILE, hist)
    except Exception:
        pass


def _is_standalone_disk(dev):
    """判断 dev 是否为「独立盘」：整块磁盘、未挂载、未被 RAID/LVM 等持有。"""
    if not _safe_token(dev):
        return False, "非法设备名"
    # 注意：必须加 -d（只看该设备本身），否则会递归列出其子设备（分区/RAID/LVM）的类型，
    # 导致即便顶层是 disk 也被误判为「非整块磁盘」。
    typ = run_cmd(["lsblk", "-ndno", "TYPE", "/dev/%s" % dev], 3).strip()
    if typ != "disk":
        return False, "%s 不是整块磁盘（类型：%s）" % (dev, typ or "未知")
    try:
        tree = json.loads(run_cmd(["lsblk", "-J", "-o", "NAME,MOUNTPOINT,TYPE", "/dev/%s" % dev], 3) or "{}")
    except Exception:
        tree = {}
    mounted = False
    def _has_mount(node):
        if node.get("mountpoint"):
            return True
        for c in node.get("children", []):
            if _has_mount(c):
                return True
        return False
    for node in tree.get("blockdevices", []):
        if _has_mount(node):
            mounted = True
            break
    if mounted:
        return False, "%s 或其分区已被挂载，无法执行破坏性测试" % dev
    holders = []
    try:
        holders = os.listdir("/sys/block/%s/holders/" % dev)
    except Exception:
        pass
    if holders:
        return False, "%s 正被其他块设备持有（%s），可能是 RAID/LVM 成员" % (dev, ", ".join(holders))
    return True, ""


# 硬盘自检前后的 SMART 关键属性快照：用于历史记录里"重映射/待处理/不可校正"前后对比
_SMART_HISTORY_IDS = (5, 197, 198)
def _smart_short_attrs(dev):
    """读 dev 的 SMART ID 5/197/198 RAW_VALUE，返回 {5:int, 197:int, 198:int}；失败/无值返回 {}。"""
    if not _safe_token(dev):
        return {}
    try:
        out = sudo_cmd([SMARTCTL, "-n", "standby", "-A", "/dev/%s" % dev], 8)
    except Exception:
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            aid = int(parts[0])
        except ValueError:
            continue
        if aid in _SMART_HISTORY_IDS:
            try:
                result[aid] = int(parts[-1])  # RAW_VALUE 是行末数字
            except ValueError:
                pass
    return result


def _set_disk_test_done(dev, message, error=None):
    with DISK_TEST_LOCK:
        job = DISK_TEST_JOBS.get(dev)
        if not job:
            return
        job["state"] = "aborted" if error == "用户中止" else ("error" if error else "done")
        job["message"] = message
        job["error"] = error
        job["elapsed"] = int(time.time() - job["started_at"])
    # 追加历史记录（写文件放锁外，避免长 IO 卡住自检轮询）
    try:
        _append_disk_test_history({
            "dev": dev,
            "type": job.get("type", "long"),
            "state": job["state"],
            "message": message,
            "error": error,
            "elapsed": job["elapsed"],
            "bad_blocks": len(job.get("bad_blocks") or []),
            "bad_lbas": (job.get("bad_blocks") or [])[:20],  # 坏块 LBA 位置（最多 20 个，前端可显示）
            "total_bytes": job.get("total_bytes") or 0,      # 扫描容量（字节）
            "smart_before": job.get("smart_before") or {},   # 扫描前 SMART 关键属性快照
            "smart_after": _smart_short_attrs(dev),          # 扫描后 SMART 关键属性快照
            "result": job.get("result"),
            "finished_at": int(time.time()),
        })
    except Exception:
        pass


def _abort_smart_long(dev):
    sudo_cmd([SMARTCTL, "-X", "/dev/%s" % dev], 10)
    _set_disk_test_done(dev, "SMART 长自检已中止", error="用户中止")


def _start_smart_long(dev):
    out = sudo_cmd([SMARTCTL, "-t", "long", "/dev/%s" % dev], 30)
    if "invalid" in out.lower() or ("error" in out.lower() and "abort" not in out.lower()):
        if "in progress" not in out.lower():
            raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else "smartctl 启动自检失败")
    with DISK_TEST_LOCK:
        DISK_TEST_JOBS[dev] = {
            "type": "long",
            "state": "running",
            "started_at": time.time(),
            "message": "SMART 长自检已启动，正在轮询进度",
            "progress": 0,
            "elapsed": 0,
            "eta_total": None,
            "eta_remain": None,
            "result": None,
            "error": None,
        }
    _threading.Thread(target=_smart_long_worker, args=(dev,), daemon=True).start()


def _smart_long_worker(dev):
    dev_path = "/dev/%s" % dev
    last_poll = 0
    while True:
        with DISK_TEST_LOCK:
            job = DISK_TEST_JOBS.get(dev)
        if not job or job.get("state") != "running":
            break
        now = time.time()
        if now - last_poll < 20:
            time.sleep(2)
            continue
        last_poll = now
        try:
            cap = sudo_cmd([SMARTCTL, "-c", dev_path], 15)
            m = re.search(r"Self-test routine in progress.*? (\d+)% remaining", cap, re.I | re.S)
            if m:
                rem = int(m.group(1))
                prog = max(0, min(100, 100 - rem))
                with DISK_TEST_LOCK:
                    j = DISK_TEST_JOBS.get(dev)
                    if j and j["state"] == "running":
                        j["progress"] = prog
                        j["message"] = "SMART 长自检进行中，剩余 %d%%" % rem
                        j["elapsed"] = int(now - j["started_at"])
                        if prog > 0.5:
                            _eta = (now - j["started_at"]) / (prog / 100.0)
                            j["eta_total"] = int(_eta)
                            j["eta_remain"] = max(0, int(_eta - (now - j["started_at"])))
                continue
            log_out = sudo_cmd([SMARTCTL, "-l", "selftest", dev_path], 15)
            lines = [l for l in log_out.splitlines() if re.match(r"^\s*\d+", l.strip())]
            if lines:
                parts = lines[0].split()
                status = " ".join(parts[2:-2]) if len(parts) >= 4 else ""
                if "completed without error" in status.lower():
                    _set_disk_test_done(dev, "SMART 长自检完成，无错误")
                elif "aborted" in status.lower() or "interrupted" in status.lower():
                    _set_disk_test_done(dev, "SMART 长自检已中止", error="用户中止")
                else:
                    _set_disk_test_done(dev, "SMART 长自检结束：%s" % status, error=status if "error" in status.lower() else None)
            else:
                _set_disk_test_done(dev, "SMART 长自检完成")
        except Exception as e:
            _set_disk_test_done(dev, "SMART 长自检异常：%s" % e, error=str(e))
            break
        time.sleep(5)


def _estimate_badblocks_eta(dev, passes=4, readonly=False):
    """根据容量和 SSD/HDD 给 badblocks 一个保守参考总用时（秒）。
    destructive（-wsv）默认 4 pass 覆写；surface（-sv）1 pass 只读。"""
    try:
        size_bytes = int(subprocess.check_output(["blockdev", "--getsize64", "/dev/%s" % dev], text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:
        return None
    try:
        with open("/sys/block/%s/queue/rotational" % dev) as f:
            is_hdd = f.read().strip() == "1"
    except Exception:
        is_hdd = True
    # 保守速度：只读 HDD 约 150MB/s、SSD 约 400MB/s；覆写 HDD 约 50MB/s、SSD 约 120MB/s
    if readonly:
        speed = 150 * 1024 * 1024 if is_hdd else 400 * 1024 * 1024
    else:
        speed = 50 * 1024 * 1024 if is_hdd else 120 * 1024 * 1024
    if size_bytes <= 0 or speed <= 0:
        return None
    return int(size_bytes * passes / speed)


def _start_badblocks(dev, mode="destructive"):
    """启动 badblocks 扫描。
    mode=destructive：-wsv 读写覆盖（破坏性，仅独立盘）；mode=surface：-sv 只读（可在用盘跑）。
    两种模式都用 -o 把坏块 LBA 列表写到 /tmp，供前端扇区网格标红。"""
    if mode == "destructive":
        ok, reason = _is_standalone_disk(dev)
        if not ok:
            raise RuntimeError(reason)
    dev_path = "/dev/%s" % dev
    try:
        total_bytes = int(subprocess.check_output(["blockdev", "--getsize64", dev_path], text=True, stderr=subprocess.DEVNULL).strip())
    except Exception:
        total_bytes = 0
    outfile = "/tmp/nasdash_bb_%s.txt" % dev
    try:
        if os.path.exists(outfile):
            os.remove(outfile)
    except Exception:
        pass
    blocksize = 4096
    if mode == "surface":
        passes = 1
        cmd = [BADBLOCKS, "-sv", "-b", str(blocksize), "-o", outfile, dev_path]
        msg = "只读表面扫描已启动（不写数据，可在用盘上运行）"
        hint = _estimate_badblocks_eta(dev, passes=1, readonly=True)
    else:
        passes = 4
        cmd = [BADBLOCKS, "-wsv", "-b", str(blocksize), "-o", outfile, dev_path]
        msg = "坏块慢扫已启动（破坏性，会覆盖全盘数据）"
        hint = _estimate_badblocks_eta(dev, passes=4)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        raise RuntimeError("启动 badblocks 失败：%s" % e)
    with DISK_TEST_LOCK:
        DISK_TEST_JOBS[dev] = {
            "type": "surface" if mode == "surface" else "badblocks",
            "state": "running",
            "started_at": time.time(),
            "message": msg,
            "progress": 0,
            "elapsed": 0,
            "eta_total": None,
            "eta_remain": None,
            "eta_total_hint": hint,
            "result": None,
            "error": None,
            "pid": proc.pid,
            "blocksize": blocksize,
            "total_bytes": total_bytes,
            "smart_before": _smart_short_attrs(dev),  # 扫描前 SMART 关键属性快照（重映射/待处理/不可校正）
            "bad_blocks": [],
            "outfile": outfile,
        }
    _threading.Thread(target=_badblocks_worker, args=(dev, proc, passes, outfile), daemon=True).start()


def _badblocks_worker(dev, proc, passes=4, outfile=None):
    try:
        buf = ""
        pass_completed = 0
        total_passes = passes
        seen_bad = set()

        def _collect_bad(outf):
            """从 badblocks -o 文件收集坏块 LBA（可能扫描过程中实时写入，也可能只在结束时落盘）。"""
            nonlocal seen_bad
            try:
                with open(outf) as f:
                    for line in f:
                        line = line.strip()
                        if line.isdigit():
                            seen_bad.add(int(line))
                if seen_bad:
                    with DISK_TEST_LOCK:
                        j = DISK_TEST_JOBS.get(dev)
                        if j:
                            j["bad_blocks"] = sorted(seen_bad)
            except Exception:
                pass

        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            tail = buf[-8192:]
            # badblocks -s 用退格符原地覆盖刷新进度，buf 里会堆满历史行，
            # re.search 会永远命中最早的 0.00% —— 必须 findall 取最后一个。
            ms = re.findall(r"([\d.]+)% done", tail)
            cur = float(ms[-1]) if ms else None
            pc = buf.count("Pass completed")
            if pc > pass_completed:
                pass_completed = pc
            if cur is not None:
                # 把多个 pass 各自的 0~100% 折算成整体进度，避免 ETA 被低估、进度条回弹
                overall = (pass_completed + cur / 100.0) / total_passes * 100.0
                overall = max(0.0, min(100.0, overall))
                now = time.time()
                with DISK_TEST_LOCK:
                    j = DISK_TEST_JOBS.get(dev)
                    if j and j["state"] == "running":
                        j["progress"] = round(overall, 1)
                        j["elapsed"] = int(now - j["started_at"])
                        if overall > 0:
                            total_eta = j["elapsed"] / (overall / 100.0)
                            j["eta_total"] = int(total_eta)
                            j["eta_remain"] = max(0, int(total_eta - j["elapsed"]))
            ems = re.findall(r"\((\d+)/(\d+)/(\d+) errors\)", tail)
            if ems:
                read_err, write_err, corr = ems[-1]
                with DISK_TEST_LOCK:
                    j = DISK_TEST_JOBS.get(dev)
                    if j:
                        j["result"] = {"read_errors": read_err, "write_errors": write_err, "corrected": corr}
            if outfile:
                _collect_bad(outfile)
        rc = proc.wait()
        if outfile:
            _collect_bad(outfile)
        with DISK_TEST_LOCK:
            j = DISK_TEST_JOBS.get(dev)
        if j and j["state"] == "running":
            bad_n = len(j.get("bad_blocks") or [])
            prefix = "表面扫描" if total_passes == 1 else "坏块慢扫"
            if rc == 0:
                _set_disk_test_done(dev, "%s完成，未发现坏块" % prefix)
            elif rc == 1:
                _set_disk_test_done(dev, "%s完成，发现 %d 个坏块" % (prefix, bad_n))
            else:
                _set_disk_test_done(dev, "%s退出（返回码 %d）" % (prefix, rc), error="返回码 %d" % rc)
    except Exception as e:
        _set_disk_test_done(dev, "扫描异常：%s" % e, error=str(e))


def _abort_badblocks(dev):
    with DISK_TEST_LOCK:
        job = DISK_TEST_JOBS.get(dev)
        pid = job.get("pid") if job else None
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(0.5)
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    _set_disk_test_done(dev, "扫描已中止", error="用户中止")


_DISKS_CACHE = {"t": 0.0, "v": None}
_DISKS_TTL = 60
_DISKS_LOCK = _threading.Lock()

def get_disks(force=False):
    """硬盘 SMART 统一采集（60s TTL 缓存 + force 支持）。
    get_disks 全量 smartctl 扫盘很重（每盘最多 20s 超时），此前 /api/all、/api/raid、/api/disks、
    /api/metrics 等各自调用都会重复扫盘。统一为一份快照共享；force=True 强制重扫
    （用户手动「立即刷新」/硬盘自检完成后，需看最新 SMART 数据时）。"""
    now = time.time()
    c = _DISKS_CACHE
    if not force and c["v"] is not None and now - c["t"] <= _DISKS_TTL:
        return c["v"]
    if force:
        with _DISKS_LOCK:
            c["v"] = _collect_disks_full()
            c["t"] = time.time()
        return c["v"]
    if _DISKS_LOCK.acquire(blocking=False):
        try:
            if c["v"] is None or now - c["t"] > _DISKS_TTL:
                c["v"] = _collect_disks_full()
                c["t"] = time.time()
        finally:
            _DISKS_LOCK.release()
        return c["v"]
    return c["v"] or []

def _collect_disks_full():
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
    def _collect_one_disk(name, info, rpm_map):
        dev = f"/dev/{name}"
        size_b = info.get("size_b", "0")
        try:
            gb = int(size_b) / 1e9
            size_str = f"{gb/1000:.1f}T" if gb >= 1000 else f"{gb:.0f}G"
        except:
            size_str = "?"
        smart_out = sudo_cmd([SMARTCTL, "-n", "standby", "-a", dev], 20)
        # 偶发超时/总线竞争导致空输出时重试一次；STANDBY 状态不重试，避免唤醒休眠盘。
        if not smart_out and "STANDBY" not in smart_out.upper():
            smart_out = sudo_cmd([SMARTCTL, "-n", "standby", "-a", dev], 20)
        disk = {
            "dev": name, "size": size_str, "rota": info.get("rota", "?"),
            "model": "", "serial": "", "tran": info.get("tran", ""), "vendor": "",
            "type": "ata", "health": "N/A", "health_ok": False, "asleep": False,
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
        # 休眠盘修正：smartctl -n standby 不会唤醒休眠盘，返回含 STANDBY 的提示且无健康行。
        # 此前被兜底成 health="N/A" / health_ok=False，导致健康卡误标红（与温度卡“休眠灰显”矛盾）。
        # 休眠是正常的省电状态，≠ 坏盘：标 asleep、health=休眠、health_ok=True，避免无谓报警。
        if smart_out and "STANDBY" in smart_out.upper() \
           and not re.search(r"overall-health|SMART Health Status|SMART/Health Information", smart_out):
            disk["asleep"] = True
            disk["health"] = "休眠"
            disk["health_ok"] = True
            disk["temp"] = None
        else:
            disk["asleep"] = disk.get("asleep", False)
            disk["health_ok"] = disk["health"].upper() in ("OK", "PASSED")
        disk["standalone"], disk["standalone_reason"] = _is_standalone_disk(name)
        return disk

    # 并行采集：每块盘的 smartctl 相互独立，多线程同时跑，首屏 /api/all 不再被逐盘串行拖慢。
    if devnames:
        try:
            from concurrent.futures import ThreadPoolExecutor
            _w = min(4, len(devnames))
            with ThreadPoolExecutor(max_workers=_w) as _ex:
                disks = list(_ex.map(
                    lambda n: _collect_one_disk(n, linfo.get(n, {}), rpm_map), devnames))
        except Exception:
            disks = [_collect_one_disk(n, linfo.get(n, {}), rpm_map) for n in devnames]
    else:
        disks = []
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
    """用 lspci 的 Host bridge 设备 ID 推断芯片组系列。
    必须用 lspci -nn：plain lspci 在系统带 pciids 数据库时会打印设备中文/英文全名而不再带
    "Device XXXX"，导致正则抓不到 ID、识别成「未知」。-nn 强制输出 [vendor:device] 原始号。
    - Intel：用 [8086:xxxx] 原始号按代范围映射（不依赖 pciids 数据库，主流板不再误报未知）。
    - AMD：用 [1022:xxxx] 判定为 AMD 平台后，再用 /proc/cpuinfo 的 Ryzen 型号名推断代数
           （比易变的设备号表更稳，不会把 5600G 误标成 3000 系之类）。"""
    out = sudo_cmd(["lspci", "-nn"], 5)
    intel_hid = ""
    amd_hid = ""
    for line in out.splitlines():
        if "Host bridge" in line:
            m = re.search(r"\[8086:([0-9a-fA-F]{4})\]", line)
            if not m:
                m = re.search(r"Device ([0-9a-fA-F]{4})", line)  # 兜底：个别环境 -nn 未输出 [vendor:device]
            if m:
                intel_hid = m.group(1).lower()
                break
            m = re.search(r"\[1022:([0-9a-fA-F]{4})\]", line)
            if not m:
                m = re.search(r"Device ([0-9a-fA-F]{4})", line)
            if m and not amd_hid:
                amd_hid = m.group(1).lower()
    if intel_hid:
        hid = intel_hid
        table = {
            "190f": "100/200 系列（6/7代酷睿）", "1910": "100 系列", "1900": "100 系列（如 H110/B150）",
            "590f": "200 系列（7代）", "5910": "200 系列",
            "3e0f": "300 系列（8/9代酷睿）", "3ec2": "300 系列", "3e30": "300 系列", "3e31": "300 系列", "3e35": "300 系列",
            "3e10": "300 系列（8/9代酷睿）", "3e1f": "300 系列", "3e32": "300 系列", "3e33": "300 系列",
            "9b00": "400 系列（10代）", "9b41": "400 系列",
            "4600": "600 系列（12代）", "4601": "600 系列", "4610": "600 系列",
            "7900": "700 系列（13代）", "7a00": "700 系列", "7d00": "700 系列",
            "a700": "800 系列（14代）", "a780": "800 系列",
        }
        if hid in table:
            return "Intel " + table[hid]
        # 范围兜底：覆盖同代未逐一列举的细分型号（如 Z390 的 3e30/3e35 等 Host bridge）
        try:
            h = int(hid, 16)
        except ValueError:
            return "Intel 未知芯片组（Host bridge 0x%s）" % hid
        if 0x1900 <= h <= 0x191f or 0x5900 <= h <= 0x591f:
            return "Intel 100/200 系列（6/7代酷睿）"
        if 0x3e00 <= h <= 0x3e3f or 0x3ec0 <= h <= 0x3ecf:
            return "Intel 300 系列（8/9代酷睿）"
        if 0x9b00 <= h <= 0x9bff:
            return "Intel 400 系列（10代）"
        if 0x4c00 <= h <= 0x4cff or 0x9a00 <= h <= 0x9aff:
            return "Intel 500 系列（11代）"
        if 0x4600 <= h <= 0x46ff:
            return "Intel 600 系列（12代）"
        if 0x7900 <= h <= 0x79ff or 0x7a00 <= h <= 0x7aff or 0x7d00 <= h <= 0x7dff:
            return "Intel 700 系列（13代）"
        if 0xa700 <= h <= 0xa7ff or 0xa780 <= h <= 0xa78f:
            return "Intel 800 系列（14代）"
        # 15 代及更新的未知号（Arrow Lake 等 0xa800+/0xb000+）广谱兜底，避免再掉「未知」
        if 0xa000 <= h <= 0xafff or 0xb000 <= h <= 0xbfff:
            return "Intel 较新系列芯片组（14/15代及以后）"
        return "Intel 未知芯片组（Host bridge 0x%s）" % hid
    if amd_hid:
        return _amd_chipset_label()
    return ""


def _amd_chipset_label():
    """AMD 平台：读 /proc/cpuinfo 的 Ryzen 型号名推断代数（稳定、不易错）。"""
    cpu = ""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    if "Ryzen" in cpu:
                        break
    except Exception:
        cpu = ""
    m = re.search(r"Ryzen\s+\d*\s*(\d{4})", cpu)
    if m:
        d = m.group(1)[0]  # 型号首数字：1→1代, 3→3000, 5→5000, 7→7000, 9→9000 ...
        return {
            "1": "AMD Ryzen 1000 系列平台（Zen）",
            "2": "AMD Ryzen 2000 系列平台（Zen+）",
            "3": "AMD Ryzen 3000 系列平台（Zen 2）",
            "4": "AMD Ryzen 4000 APU 平台（Zen 2）",
            "5": "AMD Ryzen 5000 系列平台（Zen 3）",
            "7": "AMD Ryzen 7000 系列平台（Zen 4）",
            "8": "AMD Ryzen 8000 APU 平台（Zen 4）",
            "9": "AMD Ryzen 9000 系列平台（Zen 5）",
        }.get(d, "AMD 平台（具体芯片组未知）")
    return "AMD 平台（具体芯片组未知）"


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
        "note": "",
    }

    # DMI 未写入厂商信息：提示用户用芯片组推断（已取消手动标注功能）
    if not b["manufacturer"] or not b["product"]:
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


# 常见主板传感器温度测点中文名（保留原始名在 raw 字段，方便高级用户核对数据手册）
_TEMP_NAME_ZH = {
    # Nuvoton NCT67xx 常见测点
    "SYSTIN": "主板温度",
    "CPUTIN": "主板·CPU 区域",
    "AUXTIN0": "扩展温度探头 0",
    "AUXTIN1": "扩展温度探头 1",
    "AUXTIN2": "扩展温度探头 2",
    "AUXTIN3": "扩展温度探头 3",
    "AUXTIN4": "扩展温度探头 4",
    "AUXTIN5": "扩展温度探头 5",
    "PECI Agent 0": "CPU PECI 代理 0",
    "PECI Agent 1": "CPU PECI 代理 1",
    # PCH / 芯片组相关（部分主板传感器芯片会额外暴露）
    "PCH_CHIP_TEMP": "PCH 芯片组温度",
    "PCH_CHIP_CPU_MAX_TEMP": "PCH 芯片组最高温度",
    "PCH_CPU_TEMP": "PCH CPU 温度",
    "PCH_MCH_TEMP": "PCH 内存控制器温度",
    # 内存 / 代理
    "Agent0 Dimm0": "内存 DIMM0 温度",
    "Agent0 Dimm1": "内存 DIMM1 温度",
    "Agent1 Dimm0": "内存 DIMM0 温度（通道 1）",
    "Agent1 Dimm1": "内存 DIMM1 温度（通道 1）",
    # 通用 / 其他
    "Composite": "复合温度",
    "THRM": "热敏电阻",
    "NB": "北桥温度",
    "Sensor 0": "传感器 0",
    "Sensor 1": "传感器 1",
    "Sensor 2": "传感器 2",
    "SMBUSMASTER 0": "SMBus 主控 0",
    "SMBUSMASTER 1": "SMBus 主控 1",
    "TSI0_TEMP": "TSI 温度 0",
    "TSI1_TEMP": "TSI 温度 1",
    "Tctl": "CPU 温度控制",
    "Tdie": "CPU 晶粒温度",
}


def _temp_name_zh(raw_name, chip_prefix=None):
    """把传感器原始英文名翻译成中文；Core N / Package id N / TccdN 单独处理。"""
    if raw_name in _TEMP_NAME_ZH:
        return _TEMP_NAME_ZH[raw_name]
    m = re.match(r"Core\s+(\d+)", raw_name)
    if m:
        return f"CPU 核心 {m.group(1)}"
    m = re.match(r"Tccd(\d+)", raw_name)
    if m:
        return f"CPU CCD{m.group(1)} 温度"
    m = re.match(r"Package id\s+(\d+)", raw_name)
    if m:
        return "CPU 封装温度" if m.group(1) == "0" else f"CPU 封装温度 {m.group(1)}"
    return raw_name


# ===================== RAPL 功耗 =====================
# Intel RAPL（Running Average Power Limit）通过 MSR 提供 CPU 封装/核心/非核心/内存的真实功耗。
# Linux 以 powercap 子系统暴露在 /sys/class/powercap/intel-rapl:*/ 下：
#   energy_uj           当前能量计数（微焦耳，单调递增，到 max_energy_range_uj 回绕）
#   两次采样差值 / 时间间隔 = 平均功耗（瓦）
# 支持：Intel 全系（含 G5400 等低端）+ AMD zen2+。读不到返回 None（非 Intel / 内核未挂载）。
_RAPL_BASE = "/sys/class/powercap"

def _rapl_read_energy(domain_path):
    """读某个域的 energy_uj（微焦耳）。文件缺失返回 None。"""
    try:
        with open(domain_path + "/energy_uj", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _rapl_energy_max(domain_path):
    """该域能量计数器的回绕上限（微焦耳）。读不到给个安全默认。"""
    try:
        with open(domain_path + "/max_energy_range_uj", "r") as f:
            return int(f.read().strip())
    except Exception:
        return 262143300000  # 默认 2^18 uj * 1e6，Intel 常见值

def get_rapl_power():
    """读取 CPU 封装/核心/非核心/内存实时功耗（瓦）。

    返回 {"package": w, "core": w, "uncore": w, "dram": w, "ok": bool, "total": w}；
    RAPL 不可用时返回 {"ok": False}。函数内两次读数差分（间隔 0.25s），
    不依赖跨请求状态，因此可安全地放在 get_system 的 60s 缓存里也能出数。
    """
    import time as _t
    base = _RAPL_BASE
    if not os.path.isdir(os.path.join(base, "intel-rapl:0")):
        return {"ok": False}
    paths = {
        "package": os.path.join(base, "intel-rapl:0"),
        "core": os.path.join(base, "intel-rapl:0:0"),
        "uncore": os.path.join(base, "intel-rapl:0:1"),
        "dram": os.path.join(base, "intel-rapl:0:2"),
    }
    def sample():
        out = {}
        for k, p in paths.items():
            e = _rapl_read_energy(p)
            if e is not None:
                out[k] = e
        return out
    e1 = sample()
    if not e1:
        return {"ok": False}
    _t.sleep(0.25)
    e2 = sample()
    watts = {}
    for k in ("package", "core", "uncore", "dram"):
        if k in e1 and k in e2:
            diff = e2[k] - e1[k]
            if diff < 0:  # 计数器回绕
                diff += _rapl_energy_max(paths[k])
            watts[k] = round(diff / 0.25 / 1e6, 2)
    if not watts:
        return {"ok": False}
    total = round(sum(watts.values()), 2)
    watts["ok"] = True
    watts["total"] = total
    return watts


@_ttl_cache(60)
def get_network_nics():
    """独立采集网卡列表（IP/MAC/速率/驱动/状态等），供 /api/network 轻量刷新，不进 get_system 的 60s 缓存。"""
    def _nic_hw_info(name):
        # 补充单张网卡的硬件信息：MTU / 双工 / 驱动 / 总线 / 厂商型号。
        # 全部失败也不影响其它采集（OVS 桥等虚拟口拿不到就留空）。
        info = {"mtu": "", "duplex": "", "driver": "", "bus_info": "", "model": ""}
        mtu = read_file(f"/sys/class/net/{name}/mtu").strip()
        if mtu.isdigit():
            info["mtu"] = mtu
        dup = read_file(f"/sys/class/net/{name}/duplex").strip()
        if dup:
            info["duplex"] = dup
        try:
            out = run_cmd(["ethtool", "-i", name], 3)
            for line in out.splitlines():
                if line.startswith("driver:"):
                    info["driver"] = line.split(":", 1)[1].strip()
                elif line.startswith("bus-info:"):
                    info["bus_info"] = line.split(":", 1)[1].strip()
        except Exception:
            pass
        # 总线是 PCI 地址时，用 lspci -nn 反查厂商型号（飞牛的「网卡硬件信息」）
        if info["bus_info"] and re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.", info["bus_info"], re.I):
            try:
                out = run_cmd(["lspci", "-nn", "-s", info["bus_info"]], 3)
                # 行末可能带「 (rev 10)」等后缀，故不锚定行尾；只抓「]: 」到「[厂商:设备]」之间的描述
                mh = re.search(r"\]:\s*(.+?)\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", out, re.S)
                if mh:
                    info["model"] = mh.group(1).strip()
            except Exception:
                pass
        return info

    # 网卡（只显示物理网卡 / bond / 桥接端口，过滤 docker/虚拟网桥/容器/虚拟机等噪音接口）
    link_out = run_cmd(["ip", "-o", "link", "show"], 5)
    addr_out = run_cmd(["ip", "-o", "addr", "show"], 5)
    # 先直接从 addr 输出解析「接口名 -> IP」映射：
    # - ip -o link 对 veth/peer 接口会显示 name@ifN 后缀，而 ip -o addr 用原始名（也可能带 @ifN），
    #   直接按行解析 addr 可绕开「按名回查整行」的匹配失败问题；
    # - 不同版本/系统的 `ip -o addr show` 格式并不一致：有的接口名后带冒号，有的不带；
    #   有的 inet 在接口名同行，有的 inet 在续行。用状态机维护当前接口名最稳；
    # - 同一接口可能同时有 inet6/inet，这里优先取 IPv4。
    ip4_map = {}
    ip6_map = {}
    cur_iface = None
    for aline in addr_out.splitlines():
        header = re.match(r"^\d+:\s+(\S+)", aline)
        if header:
            # 去掉末尾可能存在的冒号和 @ifN 后缀，与 link 解析出的 name 对齐
            cur_iface = header.group(1).split("@")[0].rstrip(":")
        if not cur_iface:
            continue
        im4 = re.search(r"\binet\s+(\S+)", aline)
        if im4 and cur_iface not in ip4_map:
            ip4_map[cur_iface] = im4.group(1).split("/")[0]
        im6 = re.search(r"\binet6\s+(\S+)", aline)
        if im6:
            # 同一接口可能有多条 inet6（含 fe80 链路本地），用列表收集，优先全局地址
            ip6_map.setdefault(cur_iface, []).append(im6.group(1).split("/")[0])
    # 纯虚拟/容器/虚拟机接口不计入物理网卡列表，避免「无IP」噪音干扰查看
    SKIP_NIC_PREFIX = ("lo", "docker", "br-", "veth", "ovs-system", "__tmp",
                       "flannel", "cni", "tailscale", "ts", "wg", "safeline", "vnet", "virbr")
    nics = []
    for line in link_out.splitlines():
        m = re.match(r"\d+:\s+(\S+?):\s+<([^>]*)>.*?state\s+(\w+).*?link/(\S+)\s+(\S+)", line)
        if not m:
            continue
        name = m.group(1).split("@")[0]
        if name == "lo" or name.startswith(SKIP_NIC_PREFIX):
            continue
        state = m.group(3)
        mac = m.group(5)
        speed = read_file(f"/sys/class/net/{name}/speed").strip()
        if not speed.isdigit():
            speed = ""
        ip = ip4_map.get(name, "")
        nics.append({"name": name, "state": state, "mac": mac, "speed": speed,
                      "ip": ip, "ipv6": ""})
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
    # 补充每张网卡的硬件信息（MTU / 双工 / 驱动 / 总线 / 厂商型号）与 IPv6
    for nic in nics:
        nic.update(_nic_hw_info(nic["name"]))
        v6 = ip6_map.get(nic["name"], [])
        # 优先全局地址，没有全局地址再退而求其次显示链路本地
        nic["ipv6"] = next((a for a in v6 if not a.lower().startswith("fe80:")), (v6[0] if v6 else ""))
    # 去重：同一物理网卡与其 OVS 桥/虚拟端口常共享 MAC 地址，会被识别成两条记录
    # （如 fnOS 的 eno1 与 eno1-ovs 共享 MAC；接 USB 网卡时也可能出现「物理口 + 桥」两条）。
    # 按 MAC 归并最稳健——物理口名（不含 -ovs 后缀）作展示名，IP/速率/状态/实时流量各取所长，
    # 最终页面只显示一条完整记录。纯虚拟接口（无 MAC 或唯一 MAC）保持独立。
    by_mac = {}
    _no_mac = []
    for nic in nics:
        mac = (nic.get("mac") or "").lower().strip()
        if not mac:
            _no_mac.append(nic)
            continue
        by_mac.setdefault(mac, []).append(nic)
    merged_nics = []
    for mac, grp in by_mac.items():
        if len(grp) == 1:
            merged_nics.append(grp[0])
            continue
        # 多条 → 合并：优先用物理口名（不含 -ovs）
        phys = [n for n in grp if not n["name"].endswith("-ovs")] or grp
        m = dict(phys[0])
        for n in grp:
            if not m.get("ip") and n.get("ip"):
                m["ip"] = n["ip"]
            if not m.get("speed") and n.get("speed"):
                m["speed"] = n["speed"]
            if n.get("state", "").upper() == "UP" and m.get("state", "").upper() != "UP":
                m["state"] = n["state"]
            if n.get("rx_rate") is not None:
                m["rx_rate"] = n.get("rx_rate", 0.0)
            if n.get("tx_rate") is not None:
                m["tx_rate"] = n.get("tx_rate", 0.0)
            for k in ("ipv6", "mtu", "duplex", "driver", "bus_info", "model"):
                if not m.get(k) and n.get(k):
                    m[k] = n[k]
        # 显示名优先用 OVS 桥（与 fnOS 一致：真实 IP 配在桥上），硬件字段仍取自物理口
        ovs = [n for n in grp if n["name"].endswith("-ovs")]
        if ovs:
            m["name"] = ovs[0]["name"]
        # 实时流量轮询按「物理口名」匹配 /api/metrics（指标以物理口 eno1 上报，而非 eno1-ovs）
        m["phy_name"] = phys[0]["name"].replace("-ovs", "") if phys[0]["name"].endswith("-ovs") else phys[0]["name"]
        merged_nics.append(m)
    merged_nics.extend(_no_mac)
    nics = merged_nics
    return nics

_SYSTEM_CACHE = {"t": 0.0, "v": None}
_SYSTEM_TTL = 30
_SYSTEM_LOCK = _threading.Lock()

def get_system(force=False):
    """系统总览统一采集（30s TTL 缓存 + force 支持）。
    get_system 每次跑完整采集（sensors/风扇枚举/网卡/GPU/CPU/lscpu 等），
    /api/system 与 /api/all 此前各跑一遍。统一为一份快照共享；
    force=True 强制重采（检测页手动「立即刷新」）。"""
    now = time.time()
    c = _SYSTEM_CACHE
    if not force and c["v"] is not None and now - c["t"] <= _SYSTEM_TTL:
        return c["v"]
    if force:
        with _SYSTEM_LOCK:
            c["v"] = _collect_system_full()
            c["t"] = time.time()
        return c["v"]
    if _SYSTEM_LOCK.acquire(blocking=False):
        try:
            if c["v"] is None or now - c["t"] > _SYSTEM_TTL:
                c["v"] = _collect_system_full()
                c["t"] = time.time()
        finally:
            _SYSTEM_LOCK.release()
        return c["v"]
    return c["v"] or {}

def _collect_system_full():
    d = {}
    d["hostname"] = socket.gethostname()
    d["kernel"] = platform.release()
    d["os"] = "Debian 12 (bookworm) / fnOS"
    # CPU（基础字段 + 详细字段）
    lscpu = run_cmd(["lscpu"], 5)
    def lscpu_field(name):
        m = re.search(rf"^{re.escape(name)}:\s*(.+)$", lscpu, re.MULTILINE)
        return m.group(1).strip() if m else None
    m = re.search(r"Model name:\s*(.+)", lscpu)
    d["cpu_model"] = m.group(1).strip() if m else "?"
    m = re.search(r"CPU\(s\):\s*(\d+)", lscpu)
    d["cpu_threads"] = int(m.group(1)) if m else 0
    m = re.search(r"Core\(s\) per socket:\s*(\d+)", lscpu)
    d["cpu_cores"] = int(m.group(1)) if m else 0
    m = re.search(r"CPU max MHz:\s*([\d.]+)", lscpu)
    d["cpu_freq"] = m.group(1) if m else "?"

    def _int(s, default=0):
        try:
            return int(s)
        except Exception:
            return default
    def _float(s, default=None):
        try:
            return float(s)
        except Exception:
            return default

    # 扩展 CPU 详情（架构/缓存/虚拟化等），供前端可收起面板使用
    cpuinfo = read_file("/proc/cpuinfo", "")
    first_core = {}
    for line in cpuinfo.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            first_core[k.strip()] = v.strip()
        if "processor" in line and len(first_core) > 5:
            break
    flags = first_core.get("flags", first_core.get("Features", "")).split()
    virt = None
    if "vmx" in flags:
        virt = "Intel VT-x"
    elif "svm" in flags:
        virt = "AMD-V"
    d["cpu_info"] = {
        "model": d["cpu_model"],
        "arch": lscpu_field("Architecture"),
        "vendor": lscpu_field("Vendor ID"),
        "family": lscpu_field("CPU family"),
        "model_id": lscpu_field("Model"),
        "stepping": lscpu_field("Stepping"),
        "sockets": _int(lscpu_field("Socket(s)"), 1),
        "cores_per_socket": _int(lscpu_field("Core(s) per socket"), d["cpu_cores"]),
        "threads_per_core": _int(lscpu_field("Thread(s) per core"), 1),
        "byte_order": lscpu_field("Byte Order"),
        "address_sizes": lscpu_field("Address sizes"),
        "numa_nodes": _int(lscpu_field("NUMA node(s)"), 1),
        "min_freq_mhz": _float(lscpu_field("CPU min MHz")),
        "max_freq_mhz": _float(lscpu_field("CPU max MHz")) or _float(d.get("cpu_freq")),
        "current_freq_mhz": _float(first_core.get("cpu MHz")),
        "bogomips": _float(first_core.get("BogoMIPS") or first_core.get("bogomips")),
        "l1d": lscpu_field("L1d cache"),
        "l1i": lscpu_field("L1i cache"),
        "l2": lscpu_field("L2 cache"),
        "l3": lscpu_field("L3 cache"),
        "virtualization": virt,
        "features": {
            "lm": "lm" in flags,      # Long Mode / 64-bit
            "ht": "ht" in flags,      # Hyper-Threading
            "aes": "aes" in flags,
            "avx": "avx" in flags,
            "avx2": "avx2" in flags,
            "avx512": any(f.startswith("avx512") for f in flags),
        },
    }
    # 负载
    la = read_file("/proc/loadavg").split()
    d["load"] = la[:3] if len(la) >= 3 else ["0","0","0"]
    # 实时功耗（RAPL，Intel 真实测量；不可用则为 None）
    try:
        d["power"] = get_rapl_power()
    except Exception:
        d["power"] = {"ok": False}
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
    cached = mi.get("Cached", 0) + mi.get("Buffers", 0)
    d["memory"] = {
        "total": fmt_kb(mt), "used": fmt_kb(used), "available": fmt_kb(ma),
        "percent": round(used / mt * 100, 1) if mt else 0,
        # 缓存（文件页+缓冲）：可被内核自动回收，不占"真占用"；用于界面拆分展示
        "cached": fmt_kb(cached),
        "cached_kb": cached,
    }
    st = mi.get("SwapTotal", 0); sf = mi.get("SwapFree", 0)
    d["swap"] = {"total": fmt_kb(st), "used": fmt_kb(st - sf)}
    # 传感器分类解析（温度/风扇/电压）：温度/电压/CPU 温度统一走采集循环快照（~2s 准实时），
    # 快照未就绪（进程刚启动首拍）时回退一次实时 sensors -j，保证首屏不空。
    d["sensors"] = {"temps": [], "fans": [], "voltages": []}
    cpu_temp = None
    sens_j = None
    _snap = _temp_snapshot_read()
    if _snap.get("temps"):
        d["sensors"]["temps"] = _snap["temps"]
        d["sensors"]["voltages"] = _snap["voltages"]
        cpu_temp = _snap.get("cpu_temp")
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
            _t, _v = _parse_sensors_all(j)
            d["sensors"]["temps"] = _t
            d["sensors"]["voltages"] = _v
            if cpu_temp is None:
                cpu_temp = _parse_cpu_temp(j)
        except (json.JSONDecodeError, ValueError):
            pass
    # 风扇卡片：唯一来源 = 本机可控制风扇全集（_enumerate_fans，与温控循环 / 风扇专页同一份）。
    # 仅枚举有 pwm 的通道，故华硕双芯片主板(asus-ec 无 pwm)不会生成幽灵卡；
    # 默认名带芯片前缀（如 nct6798·风扇1），从根上杜绝重名（论坛 #1 反馈：两个 FAN1）。
    # rpm 直接从同 hwmon 目录的 fan{idx}_input 读（移植 FanControlServerApp 同思路），不依赖 sensors 解析。
    for (_hw, _fi) in _enumerate_fans():
        _chip = read_file(f"{_hw}/name").strip() or os.path.basename(_hw)
        _pe = read_file(f"{_hw}/pwm{_fi}_enable").strip()
        _pv = read_file(f"{_hw}/pwm{_fi}").strip()
        _rpm_path = f"{_hw}/fan{_fi}_input"
        _rpm = 0
        if os.path.exists(_rpm_path):
            try:
                _rpm = int(read_file(_rpm_path).strip())
            except (ValueError, Exception):
                _rpm = 0
        _lab = _fan_label_for(_hw, _fi)
        display = _lab.get("name") or f"{_chip}·风扇{_fi}"
        _mm = {"0": "off", "1": "manual", "2": "auto"}
        d["sensors"]["fans"].append({
            "name": display,
            "label": _lab.get("name", ""),
            "voltage": _lab.get("voltage", ""),
            "rpm": _rpm,
            "stopped": _rpm < 1,
            "mode": _mm.get(_pe, ""),
            "pwm": round(int(_pv) / 255 * 100) if _pv else None,
            "controllable": bool(_pe),
            "hwmon": _hw,
            "idx": _fi,
            "has_tach": _rpm > 0,
            # 用户标注：该口接的是 2/3 针无转速反馈线风扇，读不到转速属正常（非空口/非故障）
            "no_tach": bool(_lab.get("no_tach")),
            "hidden": bool(_lab.get("hidden")),
        })
    # 合并风扇控速元数据（温控规则来源 / 逻辑模式 / 计算目标 / 目标占空比），
    # 供前端首屏即正确显示「温度联动控速」来源与逻辑模式（手动 / 自动·CPU·主板·硬盘温控），
    # 而非仅按硬件寄存器位误报。否则下拉框恒显「关闭（手动/默认）」、状态标签错显（#2/#3 回归）。
    try:
        _fs = get_fan_status()
        _fs_map = {}
        for _ff in _fs:
            _fs_map["%s::%d" % (_ff.get("hwmon"), _ff.get("idx"))] = _ff
        for _f in d["sensors"]["fans"]:
            _m = _fs_map.get("%s::%d" % (_f.get("hwmon"), _f.get("idx")))
            if not _m:
                continue
            _f["mode"] = _m.get("mode", _f.get("mode"))
            # pwm_mode 只有 get_fan_status() 会读 sysfs，首屏必须合并进来，
            # 否则前端 `${f.pwm_mode?...}` 判空 → PWM/DC 下拉框与「无反馈线+PWM」告警首屏不渲染。
            _f["pwm_mode"] = _m.get("pwm_mode")
            _f["rule_source"] = _m.get("rule_source")
            _f["rule"] = _m.get("rule")
            _f["computed_pwm"] = _m.get("computed_pwm")
            _f["target_pct"] = _m.get("target_pct")
            _f["has_curve"] = _m.get("has_curve")
            _f["manual_active"] = _m.get("manual_active")
            _f["active_mode"] = _m.get("active_mode")
    except Exception:
        pass
    d["cpu_temp"] = cpu_temp
    # 兼容旧字段
    d["temps"] = {t["name"]: t["value"] for t in d["sensors"]["temps"]}
    # 显卡：lspci 基础信息 + nvidia-smi / sysfs 补充显存/时钟/温度
    def _to_int(s):
        try:
            return int(str(s).split()[0])
        except Exception:
            return None
    def _to_float1(s):
        """转浮点并保留 1 位小数（用于功耗等带小数的读数），失败返回 None。"""
        try:
            return round(float(str(s).split()[0]), 1)
        except Exception:
            return None
    # AMD 显卡设备 ID → (显存位宽, 显存类型) 查表。
    # amdgpu 驱动无标准 sysfs 暴露位宽（只有 mem_info_vram_* 容量，位宽仅出现在带宽公式注释里），
    # 用 lspci 设备号查表是实用解法。覆盖常见 RX 5000/6000 系列（RDNA1/RDNA2 桌面独显）；
    # 未在表内的仍显示「—」，并提示型号未收录。
    # 数据来源：各卡官方规格（设备 ID 取自 lspci -nn，与 macOS「系统信息」设备 ID 一致）。
    _AMD_GPU_SPECS = {
        # Navi 23 (RDNA2)
        "73ff": (128, "GDDR6"),  # RX 6600 / 6600 XT
        "73ef": (128, "GDDR6"),  # RX 6650 XT
        "73e3": (128, "GDDR6"),  # Pro W6600
        # Navi 24 (RDNA2)
        "743f": (64, "GDDR6"),   # RX 6400
        "7440": (64, "GDDR6"),   # RX 6500 XT
        # Navi 22 (RDNA2)
        "73df": (192, "GDDR6"),  # RX 6700 XT（非 XT 10GB 版为 160-bit，此处按最常见 XT）
        # Navi 21 (RDNA2)
        "73bf": (256, "GDDR6"),  # RX 6800 / 6800 XT / 6900 XT
        "73af": (256, "GDDR6"),  # RX 6900 XT (XTXH)
        "73a5": (256, "GDDR6"),  # RX 6950 XT
        "73a3": (256, "GDDR6"),  # Pro W6800
        # Navi 10 (RDNA1)
        "731f": (256, "GDDR6"),  # RX 5700 / 5700 XT
        "7312": (256, "GDDR6"),  # Pro W5700
        "7310": (256, "GDDR6"),  # RX 5700 (Lite)
        # Navi 14 (RDNA1)
        "7340": (128, "GDDR6"),  # RX 5500 XT / 5300
        "7341": (128, "GDDR6"),  # Pro W5500
    }

    def _amd_bus_width(dev):
        """AMD 独显：按 lspci 设备 ID 查显存位宽与类型。返回 (bit_width, mem_type)，查不到为 (None,None)。"""
        if not dev:
            return (None, None)
        dev = str(dev).lower()
        if dev.startswith("0x"):
            dev = dev[2:]
        spec = _AMD_GPU_SPECS.get(dev)
        return spec if spec else (None, None)

    def _short_pci(bus_id):
        if not bus_id:
            return ""
        return ":".join(bus_id.split(":")[1:])  # 00000000:01:00.0 -> 01:00.0
    def _collect_nvidia():
        """NVIDIA 专用：一次性拿到型号/显存/核心频率/显存频率/温度，位宽与显存类型从 -q 补。无驱动返回 []。"""
        out = []
        smi = run_cmd(["which", "nvidia-smi"], 2).strip()
        if not smi:
            return out
        q = run_cmd([smi, "--query-gpu=index,pci.bus_id,name,memory.total,"
                     "clocks.current.graphics,clocks.current.memory,temperature.gpu,"
                     "power.draw,power.limit,driver_version", "--format=csv,noheader,nounits"], 8)
        if not q.strip():
            return out
        for line in q.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 8:
                continue
            idx, bus, name, mem, gclk, mclk, temp = parts[:7]
            pdraw = parts[7] if len(parts) > 7 else None
            plimit = parts[8] if len(parts) > 8 else None
            drv = parts[9] if len(parts) > 9 else None
            info = {"pci": bus, "name": name, "driver": "nvidia", "driver_ver": drv,
                    "memory_total": _to_int(mem), "core_clock": _to_int(gclk),
                    "mem_clock": _to_int(mclk), "temp": _to_int(temp),
                    "power_draw": _to_float1(pdraw), "power_cap": _to_float1(plimit)}
            # 位宽 / 显存类型：-q 补充（部分驱动字段缺失则留空，不报错）
            try:
                qd = run_cmd([smi, "-q", "-i", idx], 8)
                bw = re.search(r"Bus Width\s*:\s*(\d+)\s*bit", qd, re.I)
                if bw:
                    info["bus_width"] = int(bw.group(1))
                mt = re.search(r"Memory Type\s*:\s*(\S+)", qd)
                if mt:
                    info["mem_type"] = mt.group(1)
            except Exception:
                pass
            out.append(info)
        return out
    def _gpu_temp_from_sysfs(pci):
        """按 PCI 地址在 /sys/class/drm/cardN/device/hwmon 读 GPU 温度（独显/AMD）。"""
        try:
            base = _short_pci(pci)
            for name in os.listdir("/sys/class/drm"):
                if not re.match(r"^card\d+$", name):
                    continue
                devdir = os.path.join("/sys/class/drm", name, "device")
                uevent = os.path.join(devdir, "uevent")
                if not os.path.exists(uevent):
                    continue
                data = open(uevent).read()
                mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
                if not mm:
                    continue
                dev = mm.group(1)
                if base and (base == _short_pci(dev) or dev.endswith(base) or base.endswith(_short_pci(dev))):
                    hdir = os.path.join(devdir, "hwmon")
                    if os.path.isdir(hdir):
                        for hw in sorted(os.listdir(hdir)):
                            for tf in ("temp1_input", "temp2_input", "temp_input"):
                                p = os.path.join(hdir, hw, tf)
                                if os.path.exists(p):
                                    try:
                                        return int(open(p).read().strip()) // 1000
                                    except Exception:
                                        pass
        except Exception:
            return None
        return None

    def _amd_read_clocks(pci):
        """AMD 独显：读核心频率(SCLK)/显存频率(MCLK)。优先 pp_dpm_sclk/mclk 当前档(*)，回退 hwmon freq1/freq2_input(Hz)。
        返回 (core_clock_MHz, mem_clock_MHz)，读不到为 None。amdgpu 无标准接口暴露显存位宽，位宽由调用方保持 None。"""
        core = mem = None
        try:
            base = _short_pci(pci)
            target = None
            for name in os.listdir("/sys/class/drm"):
                if not re.match(r"^card\d+$", name):
                    continue
                devdir = os.path.join("/sys/class/drm", name, "device")
                uevent = os.path.join(devdir, "uevent")
                if not os.path.exists(uevent):
                    continue
                mm = re.search(r"PCI_SLOT_NAME=(\S+)", open(uevent).read())
                if not mm:
                    continue
                dev = mm.group(1)
                if base and (base == _short_pci(dev) or dev.endswith(base) or base.endswith(_short_pci(dev))):
                    target = devdir
                    break
            if not target:
                return (None, None)
            # SCLK / MCLK 当前档（带 * 标记）
            sclk_f = os.path.join(target, "pp_dpm_sclk")
            if os.path.exists(sclk_f):
                for line in open(sclk_f).read().splitlines():
                    if "*" in line:
                        sm = re.search(r":\s*([\d.]+)\s*Mhz", line, re.I)
                        if sm:
                            core = int(round(float(sm.group(1))))
                            break
            mclk_f = os.path.join(target, "pp_dpm_mclk")
            if os.path.exists(mclk_f):
                for line in open(mclk_f).read().splitlines():
                    if "*" in line:
                        mm2 = re.search(r":\s*([\d.]+)\s*Mhz", line, re.I)
                        if mm2:
                            mem = int(round(float(mm2.group(1))))
                            break
            # 回退：hwmon freq1_input(GPU)/freq2_input(显存)，单位 Hz（值 >1e6 视为 Hz 再换算，避免个别内核直接给 MHz）
            if core is None or mem is None:
                hdir = os.path.join(target, "hwmon")
                if os.path.isdir(hdir):
                    for hw in sorted(os.listdir(hdir)):
                        if core is None:
                            f1 = os.path.join(hdir, hw, "freq1_input")
                            if os.path.exists(f1):
                                try:
                                    v = int(open(f1).read().strip())
                                    core = int(v / 1000000) if v > 1000000 else v
                                except Exception:
                                    pass
                        if mem is None:
                            f2 = os.path.join(hdir, hw, "freq2_input")
                            if os.path.exists(f2):
                                try:
                                    v = int(open(f2).read().strip())
                                    mem = int(v / 1000000) if v > 1000000 else v
                                except Exception:
                                    pass
        except Exception:
            pass
        return (core, mem)

    def _amd_read_power(pci):
        """AMD/Intel 独显功耗：读 amdgpu hwmon 的 power1_input(微瓦) 与 power1_cap(微瓦上限)。
        返回 (power_draw_W, power_cap_W)，读不到为 (None,None)。核显与 CPU 共用电源域，不单独统计。"""
        draw = cap = None
        try:
            base = _short_pci(pci)
            target = None
            for name in os.listdir("/sys/class/drm"):
                if not re.match(r"^card\d+$", name):
                    continue
                devdir = os.path.join("/sys/class/drm", name, "device")
                uevent = os.path.join(devdir, "uevent")
                if not os.path.exists(uevent):
                    continue
                mm = re.search(r"PCI_SLOT_NAME=(\S+)", open(uevent).read())
                if not mm:
                    continue
                dev = mm.group(1)
                if base and (base == _short_pci(dev) or dev.endswith(base) or base.endswith(_short_pci(dev))):
                    target = devdir
                    break
            if not target:
                return (None, None)
            hdir = os.path.join(target, "hwmon")
            if os.path.isdir(hdir):
                for hw in sorted(os.listdir(hdir)):
                    if draw is None:
                        p1 = os.path.join(hdir, hw, "power1_input")
                        if os.path.exists(p1):
                            try:
                                v = int(open(p1).read().strip())
                                draw = round(v / 1000000.0, 1)  # 微瓦 → 瓦
                            except Exception:
                                pass
                    if cap is None:
                        p1c = os.path.join(hdir, hw, "power1_cap")
                        if os.path.exists(p1c):
                            try:
                                v = int(open(p1c).read().strip())
                                cap = round(v / 1000000.0, 1)
                            except Exception:
                                pass
        except Exception:
            pass
        return (draw, cap)

    def _igpu_core_freq():
        """Intel/AMD 核显核心频率（MHz），读 /sys/class/drm/card*/device/drm/card*/gt_cur_freq_mhz。无则 None。"""
        try:
            for name in sorted(os.listdir("/sys/class/drm")):
                if not re.match(r"^card\d+$", name):
                    continue
                devdir = os.path.join("/sys/class/drm", name, "device")
                cand = os.path.join(devdir, "drm", name, "gt_cur_freq_mhz")
                cand2 = os.path.join(devdir, "gt_cur_freq_mhz")
                p = cand if os.path.exists(cand) else (cand2 if os.path.exists(cand2) else None)
            if p:
                return _to_int(open(p).read().strip())
        except Exception:
            return None
        return None

    def _pci_dev_from_sysfs(pci):
        """UEVENT 加固：lspci 在装了 pciids 数据库时可能只打印设备全名、不打印 [vendor:device]，
        导致上面正则抓不到设备号（dev 变空），AMD 位宽查表随之失效。
        改从内核 /sys/bus/pci/devices/*:<pci>/uevent 直接读 PCI_ID，格式恒为 'vendor:device'，
        不受 pciids 影响，最稳。返回设备号(小写)或 None。"""
        if not pci:
            return None
        try:
            import glob
            for uevent in glob.glob("/sys/bus/pci/devices/*:%s/uevent" % pci):
                for line in open(uevent):
                    if line.startswith("PCI_ID="):
                        parts = line.strip().split("=", 1)[1].split(":", 1)
                        if len(parts) == 2 and parts[1]:
                            return parts[1].lower()
        except Exception:
            return None
        return None

    def _lshw_video_map():
        """第三重兜底：lspci -nn 正则 + uevent PCI_ID 都拿不到设备号时，
        用 lshw -numeric（强制打印 [vendor:dev]，不受 pciids 库影响）按 PCI 地址补设备号与名称。
        仅在前面两路都失败才调用，避免 lshw 的耗时扫描进入常规轮询路径。结果按短 PCI 地址缓存。"""
        cache = getattr(_lshw_video_map, "_cache", {})
        if cache:
            return cache
        try:
            out = sudo_cmd(["lshw", "-C", "video", "-numeric"], 10)
            blocks, cur = [], None
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("*-"):
                    cur = {}
                    blocks.append(cur)
                    continue
                if cur is None:
                    continue
                if s.startswith("bus info:"):
                    m = re.search(r"pci@(?:[0-9a-f]{4}:)?([0-9a-f]{2}:[0-9a-f]{2}\.\d)", s, re.I)
                    if m:
                        cur["pci"] = m.group(1)
                elif s.startswith("product:"):
                    m = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", s, re.I)
                    if m:
                        cur["vendor"] = m.group(1).lower()
                        cur["dev"] = m.group(2).lower()
                    nm = s.split(":", 1)[1].strip()
                    nm = re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]\s*$", "", nm, flags=re.I)
                    cur["name"] = nm
            for b in blocks:
                if b.get("pci") and "dev" in b:
                    cache[b["pci"]] = (b.get("vendor", ""), b["dev"], b.get("name", ""))
            _lshw_video_map._cache = cache
        except Exception:
            pass
        return cache

    def _parse_pcie(out):
        """从 lspci -vvv 输出解析 PCIe 协商速率/通道。返回 dict 或 None。"""
        if not out:
            return None
        def _gen(gts):
            try:
                g = float(gts)
            except Exception:
                return None
            return {2.5: "1.0", 5.0: "2.0", 8.0: "3.0", 16.0: "4.0",
                    32.0: "5.0", 64.0: "6.0"}.get(g)
        cap = re.search(r"LnkCap:[^\n]*?Speed\s*([\d.]+)\s*GT/s[^\n]*?Width\s*(x\d+)", out)
        sta = re.search(r"LnkSta:[^\n]*?Speed\s*([\d.]+)\s*GT/s[^\n]*?Width\s*(x\d+)", out)
        if not cap and not sta:
            return None
        return {"gen_cap": _gen(cap.group(1)) if cap else None,
                "width_cap": cap.group(2) if cap else None,
                "gen_sta": _gen(sta.group(1)) if sta else None,
                "width_sta": sta.group(2) if sta else None}

    # _clean_gpu_name 已提升到模块级（见下方函数定义），此处不再内联定义。

    lspci = run_cmd(["lspci", "-nn"], 5)
    gpus = []
    has_igpu = False
    for line in lspci.splitlines():
        if not re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I):
            continue
        # PCI 地址（行首 xx:xx.x）
        pci = ""
        pm = re.match(r"^([0-9a-f]{2}:[0-9a-f]{2}\.\d)", line)
        if pm:
            pci = pm.group(1)
        # 形如：… VGA compatible controller [0300]: Intel Corporation UHD Graphics 630 [8086:3e90] (rev 02)
        m = re.search(r"controller\s*\[[0-9a-f]{4}\]:\s*(.+?)\s*\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
        if m:
            name = m.group(1).strip()
            vendor = m.group(2).lower()
            dev = m.group(3).lower()
            # pciids 无名字时 lspci 只打印 "Device"，用厂商+设备号兜底
            if name == "Device" or name.endswith(" Corporation Device"):
                name = {"8086": "Intel 核显", "10de": "NVIDIA 显卡",
                         "1002": "AMD 显卡"}.get(vendor, "显卡") + " (设备 %s)" % dev
        else:
            vendor = ""; name = line.strip(); dev = _pci_dev_from_sysfs(pci)
            # 第三重兜底：lspci 正则 + uevent 都拿不到设备号时，用 lshw -numeric 补
            if not dev:
                lw = _lshw_video_map().get(pci)
                if lw:
                    vendor, dev, lw_name = lw
                    if lw_name and not lw_name.lower().startswith("device") and lw_name != name:
                        name = lw_name
        if vendor == "8086" or "intel" in name.lower():
            label = "核显"; has_igpu = True
        elif vendor == "1002" and re.search(r"radeon|graphics|apu|vega|renoir|cezanne|phoenix|raphael", name, re.I):
            label = "核显"; has_igpu = True
        else:
            label = "独显"
        # 驱动（Kernel driver in use）+ PCIe 协商通道（lspci -vvv 一次拿全）
        driver = ""
        pcie = None
        if pci:
            k = run_cmd(["lspci", "-vvv", "-s", pci], 3)
            km = re.search(r"Kernel driver in use:\s*(\S+)", k)
            if km:
                driver = km.group(1)
            pcie = _parse_pcie(k)
        gpus.append({"type": label, "name": name, "vendor": vendor, "dev": dev,
                     "pci": pci, "driver": driver, "pcie": pcie, "vram": None,
                     "memory_total": None, "mem_type": None, "core_clock": None,
                     "mem_clock": None, "bus_width": None, "temp": None,
                     "power_draw": None, "power_cap": None})
    # NVIDIA：用 nvidia-smi 按 PCI 地址匹配补充
    for info in _collect_nvidia():
        match = None
        for g in gpus:
            if g["vendor"] == "10de" and g["pci"] and (
                info["pci"].endswith(g["pci"]) or g["pci"].endswith(_short_pci(info["pci"]))):
                match = g; break
        if not match:
            match = {"type": "独显", "name": info.get("name", "NVIDIA 显卡"),
                     "vendor": "10de", "dev": "", "pci": _short_pci(info.get("pci", "")),
                     "driver": "", "pcie": None, "vram": None, "memory_total": None,
                     "mem_type": None, "core_clock": None, "mem_clock": None,
                     "bus_width": None, "temp": None, "power_draw": None, "power_cap": None}
            gpus.append(match)
        for k in ("name", "memory_total", "mem_type", "core_clock",
                  "mem_clock", "bus_width", "temp", "driver", "driver_ver",
                  "power_draw", "power_cap"):
            if info.get(k) is not None:
                match[k] = info[k]
    # 非 NVIDIA 显卡：显存/温度补充
    cpu_temp = d.get("cpu_temp")
    for g in gpus:
        if g["vendor"] == "10de":
            continue
        # 驱动版本：开源 amdgpu/i915 随内核发布、无独立版本号。
        # 优先 modinfo 的 version/vermagic（标准 Linux 有），飞牛精简系统无 modinfo，回退内核版本。
        if g["driver"]:
            mv = run_cmd(["modinfo", g["driver"]], 3)
            mver = re.search(r"^(version|vermagic):\s*(\S+)", mv, re.M)
            if mver:
                g["driver_ver"] = mver.group(2)
            else:
                kv = run_cmd(["uname", "-r"], 2).strip()
                g["driver_ver"] = kv.split()[0] if kv else None
        if g["type"] == "核显":
            g["vram"] = "共享系统内存"
            # 核显核心频率（真实可读，非独显才有）
            g["core_clock"] = _igpu_core_freq()
            # 核显与 CPU 同封装，温度取 CPU 温度
            if cpu_temp is not None:
                g["temp"] = cpu_temp
        else:
            # 独显（AMD 等）：lspci 显存区间作为显存容量兜底
            if g["pci"] and g["memory_total"] is None:
                v = run_cmd(["lspci", "-v", "-s", g["pci"]], 3)
                vm = re.search(r"prefetchable.*?\[size=([^\]]+)\]", v, re.I)
                if vm:
                    g["vram"] = vm.group(1).strip()
            # 温度：sysfs hwmon 按 PCI 匹配
            if g["temp"] is None and g["pci"]:
                t = _gpu_temp_from_sysfs(g["pci"])
                if t is not None:
                    g["temp"] = t
            # AMD 独显：补核心频率 / 显存频率（amdgpu sysfs）；显存位宽/类型用设备 ID 查表
            if g["vendor"] == "1002" and g["pci"]:
                cc, mc = _amd_read_clocks(g["pci"])
                if cc is not None:
                    g["core_clock"] = cc
                if mc is not None:
                    g["mem_clock"] = mc
                bw, mt = _amd_bus_width(g["dev"])
                if bw is not None:
                    g["bus_width"] = bw
                if mt is not None and g["mem_type"] is None:
                    g["mem_type"] = mt
                pd, pc = _amd_read_power(g["pci"])
                if pd is not None:
                    g["power_draw"] = pd
                if pc is not None:
                    g["power_cap"] = pc
    # 兜底：插了独显后主板 BIOS 常自动禁用核显，lspci 扫不到。
    # 区分「CPU 本就没核显」和「CPU 带核显但被 BIOS 禁用」：前者直说无核显，后者提示未启用，避免误导。
    if not has_igpu:
        cm = d.get("cpu_model", "") or ""
        m = re.search(r"i[3579]-(\d{4,5})([A-Z]*)", cm)
        amd_g = re.search(r"Ryzen \d+ \d{3,4}G\b", cm, re.I)
        if (m and "F" not in m.group(2)) or amd_g:
            gpus.append({"type": "核显", "name": "未启用（BIOS 可能已禁用）",
                          "vendor": "", "dev": "", "pci": "", "driver": "", "pcie": None, "vram": "",
                          "memory_total": None, "mem_type": None, "core_clock": None,
                          "mem_clock": None, "bus_width": None, "temp": None,
                          "power_draw": None, "power_cap": None})
        else:
            gpus.append({"type": "无核显", "name": "",
                          "vendor": "", "dev": "", "pci": "", "driver": "", "pcie": None, "vram": "",
                          "memory_total": None, "mem_type": None, "core_clock": None,
                          "mem_clock": None, "bus_width": None, "temp": None,
                          "power_draw": None, "power_cap": None})
    # 统一精简显卡显示名：主名去厂商前缀+取方括号市场名，架构代号降为副标题
    for _g in gpus:
        if _g.get("name"):
            _sn, _sa = _clean_gpu_name(_g["name"])
            _g["name_full"] = (_sn + (" (" + _sa + ")" if _sa else "")) or _g["name"]
            _g["name"] = _sn
            _g["name_arch"] = _sa
    d["gpus"] = gpus

    d["nics"] = get_network_nics()
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
@_ttl_cache(60)
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
    out = sudo_cmd(["docker", "top", name], 5, quiet=True)
    for line in out.splitlines()[1:]:  # 跳过表头
        f = line.split()
        if len(f) >= 2 and f[1].isdigit():
            pids.append(int(f[1]))
    return pids

def _detect_ports(meta, running=False):
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
        if not running:
            parts.append("容器停止不检测端口")
        else:
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

def _host_cpu_threads():
    """逻辑核数（含超线程），用于把 docker 单核% 归一化为整机%所占比例。
    读取 /proc/cpuinfo 的 processor 行数，失败兜底返回 1。"""
    try:
        n = 0
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("processor"):
                    n += 1
        return n or 1
    except Exception:
        return 1

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

# `docker stats --no-stream` 给出的是「容器启动以来累计收/发字节」，并非实时速率。
# 若直接把累计字节当速率显示，会冒出「14 GB/s」这种假数字（远超 1Gbps 网卡上限）。
# 这里用两次采集的差值 / 时间差算出真实速率(B/s)；首采样或容器重启(累计值回落)时返回 (None, None)。
_DOCKER_NET_PREV = {}

def _docker_net_rate(name, rx, tx):
    """返回 (rx_rate_Bps, tx_rate_Bps)；首采样或容器重启(累计值回落)时返回 (None, None)。"""
    now = time.time()
    prev = _DOCKER_NET_PREV.get(name)
    rate = (None, None)
    if prev and prev["rx"] is not None and prev["tx"] is not None \
            and rx is not None and tx is not None:
        dt = now - prev["ts"]
        if 0 < dt < 600 and rx >= prev["rx"] and tx >= prev["tx"]:
            rate = ((rx - prev["rx"]) / dt, (tx - prev["tx"]) / dt)
    _DOCKER_NET_PREV[name] = {"rx": rx, "tx": tx, "ts": now}
    return rate

@_ttl_cache(5)
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
                        "running": c.get("State", {}).get("Running", False),
                        "name": nm,
                    }
                for c in containers:
                    meta = info.get(c["name"])
                    if meta:
                        c["ports"] = _detect_ports(meta, meta.get("running", False))
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
            # fnOS 的 docker stats {{.CPUPerc}} 返回的是「单逻辑核百分比」（未除总核数），
            # 而飞牛原生 Docker 面板显示的是「整机占比百分比」。
            # 例：G5400 双核四线程，容器占满 1 核时 {{.CPUPerc}}≈100%，飞牛原生≈25%。
            # 为与飞牛原生一致，这里把单核%除以逻辑核数做归一化（_threads）。
            _threads = _host_cpu_threads()
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
                rrx, rtx = _docker_net_rate(cname, rx, tx)
                for c in containers:
                    if c["name"] == cname:
                        _raw = _parse_docker_pct(cpu_s)
                        c["cpu"] = _raw / _threads if (_raw is not None and _threads > 1) else _raw
                        c["cpu_raw"] = _raw  # 保留归一化前的单核%，便于排查
                        c["mem_pct"] = _parse_docker_pct(memp_s)
                        c["mem"] = mem_s.strip()
                        c["mem_bytes"] = _docker_size_to_bytes(mem_s.split("/")[0].strip()) if "/" in mem_s else _docker_size_to_bytes(mem_s.strip())
                        c["net_rx"] = rx
                        c["net_tx"] = tx
                        c["net_rx_rate"] = rrx
                        c["net_tx_rate"] = rtx
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
@app.route("/ui/images/<path:filename>")
def ui_images(filename):
    """暴露 ui/images 下的静态图标，供页面内 <img> 引用。"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), "ui", "images"), filename)

@app.route("/")
def index():
    # no-store：防止浏览器/代理缓存 HTML，避免发版或重启后用户仍看到旧页面（曾导致 FCS 卡片永久“加载中”）
    resp = make_response(render_template(
        "index.html",
        APP_VERSION=APP_VERSION,
        ICON_DETECT_DATA=ICON_DETECT_DATA,
        ICON_SYSTEM_DATA=ICON_SYSTEM_DATA,
        ICON_HISTORY_DATA=ICON_HISTORY_DATA,
        ICON_RAID_DATA=ICON_RAID_DATA,
        ICON_HDD_DATA=ICON_HDD_DATA,
        ICON_STORAGE_DATA=ICON_STORAGE_DATA,
        ICON_FAN_DATA=ICON_FAN_DATA,
        ICON_DOCKER_DATA=ICON_DOCKER_DATA,
        ICON_AUTOMATION_DATA=ICON_AUTOMATION_DATA,
        ICON_MANUAL_DATA=ICON_MANUAL_DATA,
        ICON_ABOUT_DATA=ICON_ABOUT_DATA,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ===================== 操作手册（/manual 路由，离线可读） =====================
_MANUAL_CSS = """
:root{
  --text:#1f2933; --muted:#64748b; --bg:#f1f5f9; --card:#ffffff;
  --border:#e2e8f0; --blue:#2563eb; --code-bg:#1e293b; --code-fg:#e2e8f0;
}
* { box-sizing:border-box; }
body{ margin:0; background:var(--bg); color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.75; }
.wrap{ max-width:920px; margin:0 auto; padding:32px 20px 80px; }
h1{ font-size:26px; margin:0 0 6px; }
h2{ font-size:21px; margin:34px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--border); }
h3{ font-size:17px; margin:24px 0 8px; color:#0f172a; }
h4{ font-size:15px; margin:18px 0 6px; }
p{ margin:10px 0; }
ul,ol{ margin:10px 0; padding-left:24px; }
li{ margin:5px 0; }
a{ color:var(--blue); }
code{ background:var(--code-bg); color:var(--code-fg); padding:2px 6px; border-radius:5px;
  font-size:13px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
pre{ background:var(--code-bg); color:var(--code-fg); padding:14px; border-radius:8px;
  overflow-x:auto; font-size:13px; }
blockquote{ background:#fff7ed; border-left:4px solid #f59e0b; margin:12px 0;
  padding:10px 16px; border-radius:6px; color:#92400e; }
hr{ border:none; border-top:1px solid var(--border); margin:28px 0; }
.man-table{ border-collapse:collapse; width:100%; margin:12px 0; font-size:14px;
  display:table; overflow:visible; }
.man-table th,.man-table td{ border:1px solid var(--border); padding:8px 12px; text-align:left; }
.man-table th{ background:var(--card); font-weight:600; }
.man-table tbody tr:nth-child(even){ background:#f8fafc; }
.topbar{ position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--border);
  padding:10px 20px; font-size:13px; color:var(--muted); z-index:10; }
.topbar b{ color:var(--text); }
.hl-red{ color:#dc2626; font-weight:500; }
"""
# 应用内嵌版：去掉整页外壳/顶栏/定宽，宽度自适应面板；前端 request.args embed=1 时返回
_MANUAL_CSS_EMBED = """
.man-body{ color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; line-height:1.75; max-width:100%; }
.man-body h1{ font-size:24px; margin:0 0 8px; color:var(--text); }
.man-body h2{ font-size:19px; margin:28px 0 10px; padding-bottom:6px; border-bottom:2px solid var(--border); color:var(--text); }
.man-body h3{ font-size:16px; margin:20px 0 8px; color:var(--text); }
.man-body h4{ font-size:14px; margin:16px 0 6px; color:var(--text); }
.man-body p{ margin:10px 0; color:var(--text); }
.man-body ul,.man-body ol{ margin:10px 0; padding-left:24px; color:var(--text); }
.man-body li{ margin:5px 0; }
.man-body a{ color:var(--primary); }
.man-body code{ background:var(--fill); color:var(--text); padding:2px 6px; border-radius:5px; font-size:13px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
.man-body pre{ background:var(--fill); color:var(--text); padding:14px; border-radius:8px; overflow-x:auto; font-size:13px; }
.man-body blockquote{ background:var(--bg); border-left:4px solid var(--warning); margin:12px 0; padding:10px 16px; border-radius:6px; color:var(--text); }
.man-body hr{ border:none; border-top:1px solid var(--border); margin:28px 0; }
.man-body .man-table{ border-collapse:collapse; width:100%; margin:12px 0; font-size:14px; display:table; overflow:visible; }
.man-body .man-table th,.man-body .man-table td{ border:1px solid var(--border); padding:8px 12px; text-align:left; }
.man-body .man-table th{ background:var(--card); font-weight:600; }
.man-body .man-table tbody tr:nth-child(even){ background:var(--fill); }
.man-body .hl-red{ color:var(--danger); font-weight:500; }
"""

def _md_inline(text):
    """行内：转义 HTML + **粗体** + `代码` + ==红色== + <mark>红色</mark>。
    红色标记给「本版本新增/修复」高亮用：App 内显示红色，GitHub 原生黄底高亮。"""
    # 先抽离 <mark>...</mark>，避免被下面统一转义吃掉标签（内容内部的 **粗体** 仍会处理）
    _marks = []
    def _stash_mark(m):
        _marks.append(m.group(1))
        return '\x00M%d\x00' % (len(_marks) - 1)
    text = re.sub(r'<mark>(.+?)</mark>', _stash_mark, text, flags=re.S)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # 先吃 ==红色== 标记（避免被后面的 ** 误吃），用占位符包起来再做粗体/代码
    text = re.sub(r'==(.+?)==', lambda m: '\x00HL\x00' + m.group(1) + '\x00/HL\x00', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = text.replace('\x00HL\x00', '<span class="hl-red">').replace('\x00/HL\x00', '</span>')
    # 还原 <mark> 为红色 span（内部 **粗体** 已处理）
    for i, _m in enumerate(_marks):
        text = text.replace('\x00M%d\x00' % i, '<span class="hl-red">%s</span>' % _m)
    text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
    return text

def _render_markdown_stream(md):
    """流式版：逐行/逐块 yield HTML 片段，配合 /manual 的 chunked 响应边传边显。"""
    lines = md.split('\n')
    in_list = False; list_type = None
    table_rows = []; in_table = False

    def close_list():
        nonlocal in_list, list_type
        if in_list:
            yield '</%s>' % list_type; in_list = False; list_type = None
    def close_table():
        nonlocal in_table, table_rows
        if in_table:
            yield '</tbody></table>'; in_table = False; table_rows = []

    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        # 表格
        if s.startswith('|') and s.count('|') >= 2:
            if not in_table:
                in_table = True; table_rows = []
            cells = [c.strip() for c in s.strip('|').split('|')]
            if cells and all(set(c) <= set('-: ') and c for c in cells):
                i += 1; continue  # 分隔行
            table_rows.append(cells)
            nxt = lines[i+1].strip() if i+1 < len(lines) else ''
            if not (nxt.startswith('|') and nxt.count('|') >= 2):
                for chunk in close_list():
                    yield chunk
                yield '<table class="man-table"><thead><tr>' \
                           + ''.join('<th>%s</th>' % _md_inline(c) for c in table_rows[0]) \
                           + '</tr></thead><tbody>'
                for r in table_rows[1:]:
                    yield '<tr>' + ''.join('<td>%s</td>' % _md_inline(c) for c in r) + '</tr>'
                yield '</tbody></table>'
                in_table = False; table_rows = []
            i += 1; continue
        for chunk in close_table():
            yield chunk
        if s == '---':
            for chunk in close_list():
                yield chunk
            yield '<hr>'; i += 1; continue
        if s.startswith('#'):
            for chunk in close_list():
                yield chunk
            lvl = len(s) - len(s.lstrip('#'))
            yield '<h%d>%s</h%d>' % (lvl, _md_inline(s.lstrip('#').strip()), lvl); i += 1; continue
        if s.startswith('>'):
            for chunk in close_list():
                yield chunk
            yield '<blockquote>%s</blockquote>' % _md_inline(s.lstrip('>').strip()); i += 1; continue
        st = line.lstrip()
        if st.startswith('- ') or st.startswith('* '):
            if not in_list or list_type != 'ul':
                for chunk in close_list():
                    yield chunk
                yield '<ul>'; in_list = True; list_type = 'ul'
            yield '<li>%s</li>' % _md_inline(st[2:].strip()); i += 1; continue
        m = re.match(r'^\d+\.\s+(.*)$', st)
        if m:
            if not in_list or list_type != 'ol':
                for chunk in close_list():
                    yield chunk
                yield '<ol>'; in_list = True; list_type = 'ol'
            yield '<li>%s</li>' % _md_inline(m.group(1)); i += 1; continue
        if not s:
            for chunk in close_list():
                yield chunk
            i += 1; continue
        for chunk in close_list():
            yield chunk
        yield '<p>%s</p>' % _md_inline(s); i += 1
    for chunk in close_list():
        yield chunk
    for chunk in close_table():
        yield chunk

@app.route("/manual")
def manual():
    p = os.path.join(APP_DIR, "docs", "使用手册.md")
    try:
        md = open(p, "r", encoding="utf-8").read()
    except Exception as e:
        resp = make_response("<h1>操作手册未找到</h1><p>%s</p>" % _md_inline(str(e)), 404)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    if request.args.get("embed") == "1":
        # 应用内嵌：仅返回片段（无 html/head/body 外壳），由前端塞进面板，停留在应用内
        # 同样走流式，前端用 reader 边收边 append，避免大手册整段等待
        head = "<style>%s</style>\n<div class=\"man-body\">" % _MANUAL_CSS_EMBED
        tail = "</div>"
    else:
        head = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                "<title>nasdash 操作手册</title><style>%s</style></head>"
                "<body><div class=\"topbar\">nasdash 操作手册 · <b>v%s</b> · "
                "<a href=\"/\">← 返回面板</a></div>"
                "<div class=\"wrap\">") % (_MANUAL_CSS, APP_VERSION)
        tail = "</div></body></html>"
    # no-store：手册走网关反代，不缓存否则首次打开可能拿到空白/旧响应（同 / 路由）
    # 流式（chunked）：后端边渲染边推给网关/浏览器，手册长也不卡在「加载中」
    def gen():
        yield head
        for chunk in _render_markdown_stream(md):
            yield chunk
        yield tail
    resp = Response(stream_with_context(gen()), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Accel-Buffering"] = "no"  # 关掉反代缓冲，确保 chunked 真生效
    return resp

# ===================== 采集层：实时指标（网络吞吐 / 磁盘 I/O / CPU 功耗） =====================
# 这些指标需「两次采样差」才算速率，故由常驻 daemon 线程周期采样，/api/all 仅读最新值。
# 模式复用 fan_smooth_loop 的 daemon 线程做法。
_METRICS_LOCK = _threading.Lock()
_metrics_prev = {"net": {}, "disk": {}, "rapl": None, "rapl_t": 0.0, "cpu": None, "t": time.time()}
_metrics_cur = {"net": [], "disk": [], "cpu_usage": None, "cpu_power_w": 0.0, "cpu_power_valid": False}
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

def _read_disk_stats():
    """返回 {dev: (rd_sectors, wr_sectors, io_ticks)}，过滤分区(数字结尾)与 loop/ram。
    io_ticks = /proc/diskstats 第13列：设备累计花在 I/O 上的毫秒数；
    两次采样差 / 采样间隔毫秒 = 该盘 busy 占用率(%)。"""
    res = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                cols = line.split()
                if len(cols) < 13:
                    continue
                dev = cols[2]
                if dev.startswith(("loop", "ram")) or dev[-1].isdigit():
                    continue
                res[dev] = (int(cols[5]), int(cols[9]), int(cols[12]))
    except Exception:
        pass
    return res

# 每块盘最近一次有 I/O 的时刻，用于派生 standby（连续无 I/O 满阈值即视为待机）。
# 与调速线程判定口径一致：基于 /proc/diskstats 扇区差，轻量且不唤醒休眠盘。
_DISK_LAST_IO = {}
_DISK_STANDBY_IDLE_MIN = 5   # 分钟；与风扇休眠 idle_minutes 默认值对齐

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
    """daemon 线程：每 ~1s 采样一次，计算速率/功率/CPU 使用率并写入 _metrics_cur。
    采样窗口固定（net/disk 用真实间隔 dt，CPU 用相邻两次迭代差），不随请求并发抖动，
    因此 /api/metrics 高频拉取也不会把 CPU 窗口压成毫秒级而出现瞬时 100%/50% 尖刺。"""
    global _CPU_POWER_EMA, _cc_sched_last_check
    while True:
        try:
            t_now = time.time()
            now = int(t_now)
            with _METRICS_LOCK:
                dt = max(0.5, t_now - _metrics_prev["t"])
            # 历史趋势：每 30s 聚合写一行 SQLite（免维护，超 30 天自动清理）
            if now - _db_last_write >= 30:
                _write_history_sample()
            # 一致性检查定时调度：每 60s 在后台线程检查是否到点触发 CC（无 VD 时静默跳过）
            if now - _cc_sched_last_check >= 60:
                _cc_sched_last_check = now
                threading.Thread(target=_check_cc_schedule, daemon=True).start()
            # 网络吞吐
            net_now = _read_net_bytes()
            net_out = []
            with _METRICS_LOCK:
                prev = _metrics_prev["net"]
                for iface, (rx, tx) in net_now.items():
                    prx, ptx = prev.get(iface, (rx, tx))
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
            disk_now = _read_disk_stats()
            disk_out = []
            with _METRICS_LOCK:
                prev = _metrics_prev["disk"]
                for dev, (rd, wr, iot) in disk_now.items():
                    prd, pwr, piot = prev.get(dev, (rd, wr, iot))
                    rd_rate = max(0.0, (rd - prd) * 512 / dt)
                    wr_rate = max(0.0, (wr - pwr) * 512 / dt)
                    # busy%：io_ticks 两次采样差 / 间隔毫秒(dt*1000)，钳到 [0,100]
                    busy = 0.0
                    if iot >= piot and dt > 0:
                        busy = max(0.0, min(100.0, (iot - piot) / (dt * 1000) * 100))
                    # standby：连续无 I/O（扇区计数未变）满阈值即视为待机，不唤醒盘
                    if rd != prd or wr != pwr:
                        _DISK_LAST_IO[dev] = now
                        standby = False
                    else:
                        last = _DISK_LAST_IO.get(dev)
                        standby = last is not None and (now - last) >= _DISK_STANDBY_IDLE_MIN * 60
                    disk_out.append({
                        "device": dev,
                        "read_rate": round(rd_rate, 1),   # bytes/s，前端动态格式化为 B/s/KB/s/MB/s
                        "write_rate": round(wr_rate, 1),
                        "busy": round(busy, 1),           # 占用率 %，对齐飞牛 disk[].busy
                        "standby": bool(standby),         # 待机标志，对齐飞牛 disk[].standby（diskstats 轻量代理）
                    })
                _metrics_prev["disk"] = disk_now
                _metrics_cur["disk"] = disk_out
            # CPU 使用率：固定窗口（两次迭代差，≈1s）采样，写 _CPU_USAGE_CACHE，供 /api/metrics 与 /api/system 直接读取。
            # 不再由请求各自读 /proc/stat 改共享快照——那样并发请求会把窗口压到毫秒级，产生瞬时 100%/50% 尖刺。
            try:
                idle, total = _cpu_snap()
                with _CPU_USAGE_LOCK:
                    pc = _CPU_USAGE_CACHE.get("prev")
                    if pc is not None:
                        d_total = total - pc[0]
                        d_idle = idle - pc[1]
                        if d_total > 0:
                            v = max(0.0, min(100.0, round((1.0 - d_idle / d_total) * 100, 1)))
                            _CPU_USAGE_CACHE["v"] = v
                            _CPU_USAGE_CACHE["t"] = time.time()
                    _CPU_USAGE_CACHE["prev"] = (total, idle)
            except Exception:
                pass
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
            # 记录本轮回合时间，供下一轮计算真实间隔 dt（net/disk 速率用）
            with _METRICS_LOCK:
                _metrics_prev["t"] = t_now
        except Exception:
            time.sleep(2)
        time.sleep(1)

_metrics_thread = _threading.Thread(target=metrics_collect_loop, daemon=True, name="metrics")
_metrics_thread.start()

# 统一温度采集循环（~2s）：一次 sensors -j → _TEMP_SNAP 快照，所有温度消费方共享。
_temp_thread = _threading.Thread(target=_temp_collect_loop, daemon=True, name="temp-snap")
_temp_thread.start()

# ---------------------------------------------------------------------------
# GPU 实时占用（温度 + 使用率）：供系统资源页折线图。
# 与 get_gpu（重采集，含 lspci -vvv/modinfo/显存识别）分离——这里只取"会秒级跳动"的
# 两个值，尽量走 sysfs / nvidia-smi，不在 1s 轮询里重跑重型命令。
# 身份列表(lspci)缓存 5 分钟；temp/util 采样 2 秒 memo，api_metrics 每秒读缓存即可。
# 核显使用率依赖 intel_gpu_top（fnOS 未必预装）：取不到就 util=None，前端显示"暂不可用"，不造假数据。
# ---------------------------------------------------------------------------
_GPU_IDENT_CACHE = {"t": 0.0, "data": None}
_GPU_LIVE_CACHE = {"t": 0.0, "data": []}

def _clean_gpu_name(raw):
    """把 lspci/nvidia-smi 给的长设备名精简成「主市场名 + 架构代号」。
    兼容多种真实形态：
      '00:02.0 VGA compatible controller [0300]: Intel Corporation UHD Graphics 610 (Coffee Lake-S GT1) [8086:3e90] (rev 02)'
          -> ('UHD Graphics 610', 'Coffee Lake-S GT1')
      'Intel Corporation CoffeeLake-S GT1 [UHD Graphics 610]'
          -> ('UHD Graphics 610', 'CoffeeLake-S GT1')
      'NVIDIA GeForce RTX 3080' -> ('GeForce RTX 3080', '')
      'Advanced Micro Devices, Inc. [AMD/ATI] Navi 23 [Radeon RX 6600 XT]'
          -> ('Radeon RX 6600 XT', 'Navi 23')
    中文兜底名（如 '未启用（BIOS 可能已禁用）'）原样返回。"""
    if not raw or not str(raw).strip():
        return raw, ""
    n = str(raw).strip()
    # 0) 去掉 lspci 整行前缀：'00:02.0 VGA compatible controller [0300]: '
    n = re.sub(r'^\s*[0-9a-f]{2}:[0-9a-f]{2}\.\d\s+(?:VGA compatible controller|3D controller|Display controller)\s*\[[0-9a-f]{4}\]:\s*', '', n, flags=re.I)
    # 1) 去掉厂商前缀（含 APU 的 [AMD/ATI] 标记）
    n = re.sub(r'^(Intel Corporation|Intel|NVIDIA Corporation|NVIDIA|'
               r'Advanced Micro Devices,?\s*Inc\.?\s*(\[AMD/ATI\])?|AMD/ATI|AMD)\s*',
               '', n, flags=re.I)
    n = n.strip()
    # 2) 去掉结尾的 (rev 02) / [8086:3e90] 这类尾巴
    n = re.sub(r'\s*\(rev[^)]*\)\s*$', '', n, flags=re.I)
    n = re.sub(r'\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]\s*$', '', n, flags=re.I)
    # 3) 方括号里的市场名优先（旧格式：[UHD Graphics 610]）
    mkt = None
    for mm in re.finditer(r'\[([^\]]+)\]', n):
        cand = mm.group(1).strip()
        if cand.lower() in ('amd/ati',):
            continue
        mkt = cand
        break
    arch = re.sub(r'\[[^\]]+\]', '', n).strip()
    arch = re.sub(r'\s{2,}', ' ', arch).strip()
    if mkt and mkt.lower() not in ('device',):
        return mkt, arch
    # 4) 没有方括号：把结尾的架构代号括号当作 arch，如 'UHD Graphics 610 (Coffee Lake-S GT1)'
    m2 = re.match(r'^(.*?)\s*\(([^()]+)\)\s*$', n)
    if m2:
        base = m2.group(1).strip()
        suffix = m2.group(2).strip()
        if base and not re.search(r'(laptop|notebook|rev|version|tm|oc|super|max-?q)', suffix, re.I):
            return base, suffix
    # 5) 连括号都没有、但含已知核显市场名 + 后缀代号，如 'UHD Graphics 610 CoffeeLake-S GT'
    m3 = re.match(r'^(UHD Graphics\s+\d+|HD Graphics\s+\d+)', n, re.I)
    if m3:
        base = m3.group(1).strip()
        suffix = n[m3.end():].strip(" -")
        if suffix and not re.search(r'laptop|notebook', suffix, re.I):
            return base, suffix
    return (n or str(raw)), ''


def _gpu_ident_list():
    """返回 [{vendor, dev, pci, name, type}]，缓存 5 分钟，避免每 2s 跑 lspci。"""
    now = time.time()
    if _GPU_IDENT_CACHE["data"] is not None and now - _GPU_IDENT_CACHE["t"] < 300:
        return _GPU_IDENT_CACHE["data"]
    out = []
    seen = set()
    try:
        lspci = run_cmd(["lspci"], 3)
        for line in lspci.splitlines():
            if not re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I):
                continue
            pci = ""
            pm = re.match(r"^([0-9a-f]{2}:[0-9a-f]{2}\.\d)", line)
            if pm:
                pci = pm.group(1)
            m = re.search(r"controller\s*\[[0-9a-f]{4}\]:\s*(.+?)\s*\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
            if m:
                name = m.group(1).strip(); vendor = m.group(2).lower(); dev = m.group(3).lower()
            else:
                vendor = ""; name = line.strip(); dev = ""
            if vendor == "8086" or "intel" in name.lower():
                gtype = "核显"
            elif vendor == "1002" and re.search(r"radeon|graphics|apu|vega|renoir|cezanne|phoenix|raphael", name, re.I):
                gtype = "核显"
            else:
                gtype = "独显"
            # 去重：同一 PCI 地址（或同名无地址）只计一次，避免显卡被重复识别成多张
            key = pci if pci else ("name:" + name)
            if key in seen:
                continue
            seen.add(key)
            # 清理名称：短市场名 + 架构代号，原长名留作悬停全名
            sn, sa = _clean_gpu_name(name)
            name_full = (sn + (" (" + sa + ")" if sa else "")) or name
            out.append({"vendor": vendor, "dev": dev, "pci": pci,
                        "name": sn, "name_arch": sa, "name_full": name_full, "type": gtype})
    except Exception:
        out = []
    if not out:
        out = [{"vendor": "", "dev": "", "pci": "", "name": "", "type": "无核显"}]
    _GPU_IDENT_CACHE["data"] = out
    _GPU_IDENT_CACHE["t"] = now
    return out

def _gpu_temp_from_sysfs_live(pci):
    """模块级 GPU 温度读取（按 PCI 在 /sys/class/drm/cardN/device/hwmon 找），复用与 get_gpu 一致的匹配逻辑。"""
    try:
        base = (pci or "").strip()
        for name in os.listdir("/sys/class/drm"):
            if not re.match(r"^card\d+$", name):
                continue
            devdir = os.path.join("/sys/class/drm", name, "device")
            uevent = os.path.join(devdir, "uevent")
            if not os.path.exists(uevent):
                continue
            data = open(uevent).read()
            mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
            if not mm:
                continue
            dev = mm.group(1)
            if base and (base == dev or dev.endswith(base) or base.endswith(dev)):
                hdir = os.path.join(devdir, "hwmon")
                if os.path.isdir(hdir):
                    for hw in sorted(os.listdir(hdir)):
                        for tf in ("temp1_input", "temp2_input", "temp_input"):
                            p = os.path.join(hdir, hw, tf)
                            if os.path.exists(p):
                                try:
                                    return int(open(p).read().strip()) // 1000
                                except Exception:
                                    pass
    except Exception:
        return None
    return None

def _amd_gpu_busy(pci):
    """AMD 独显使用率：/sys/class/drm/cardN/device/gpu_busy_percent（0-100）。按 PCI 匹配。"""
    try:
        base = (pci or "").strip()
        for name in os.listdir("/sys/class/drm"):
            if not re.match(r"^card\d+$", name):
                continue
            devdir = os.path.join("/sys/class/drm", name, "device")
            uevent = os.path.join(devdir, "uevent")
            if not os.path.exists(uevent):
                continue
            data = open(uevent).read()
            mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
            if not mm:
                continue
            dev = mm.group(1)
            if base and (base == dev or dev.endswith(base) or base.endswith(dev)):
                bp = os.path.join(devdir, "gpu_busy_percent")
                if os.path.exists(bp):
                    return float(open(bp).read().strip())
    except Exception:
        return None
    return None

def _read_drm_attr(fname):
    """读第一个存在的 /sys/class/drm/card*/device/<fname>，取不到返回 None。"""
    try:
        for name in sorted(os.listdir("/sys/class/drm")):
            if not re.match(r"^card\d+$", name):
                continue
            p = os.path.join("/sys/class/drm", name, "device", fname)
            if os.path.exists(p):
                return int(open(p).read().strip())
    except Exception:
        return None
    return None

def _intel_igpu_top_sample():
    """Intel 核显：用 intel_gpu_top -J 取 busy/render/video/频率/功耗/rc6。
    返回 dict；取不到则返回空 dict。"""
    import subprocess
    try:
        proc = subprocess.Popen(["intel_gpu_top", "-J", "-s", "200", "-o", "-"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buf = ""
        deadline = time.time() + 3
        j = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            buf += line
            try:
                j = json.loads(buf)
                break
            except Exception:
                continue
        try:
            proc.kill()
        except Exception:
            pass
        if not j:
            return {}
        engines = j.get("engines", {})
        busy = []
        render = None
        video = None
        for k, v in engines.items():
            b = v.get("busy")
            if isinstance(b, (int, float)):
                busy.append(b)
                if k.startswith("Render"):
                    render = b
                elif k.startswith("Video"):
                    video = b
        freq = j.get("frequency", {}).get("actual")
        power = j.get("power", {}).get("GPU")
        rc6 = j.get("rc6", {}).get("value")
        out = {}
        if busy:
            out["util"] = round(sum(busy) / len(busy), 1)
        if render is not None:
            out["render"] = round(render, 1)
        if video is not None:
            out["video"] = round(video, 1)
        if isinstance(freq, (int, float)):
            out["freq_mhz"] = round(freq, 1)
        if isinstance(power, (int, float)):
            out["power_w"] = round(power, 2)
        if isinstance(rc6, (int, float)):
            out["rc6"] = round(rc6, 1)
        return out
    except Exception:
        return {}


def _intel_igpu_util():
    """Intel 核显使用率/活动度。返回 (值, 是否代理)。
    真·使用率优先（intel_gpu_top -J 各 engine busy 平均值）；无该工具则退化为频率活动度。
    都没有返回 (None, False)。"""
    sample = _intel_igpu_top_sample()
    if sample.get("util") is not None:
        return sample["util"], False
    try:
        cur = _read_drm_attr("gt_cur_freq_mhz")
        mn = _read_drm_attr("gt_min_freq_mhz")
        mx = _read_drm_attr("gt_boost_freq_mhz") or _read_drm_attr("gt_max_freq_mhz")
        if cur is not None and mn is not None and mx is not None and mx > mn:
            return round((cur - mn) / (mx - mn) * 100, 1), True
    except Exception:
        pass
    return None, False

def _gpu_memory_bytes():
    """核显共享系统内存：返回 (used_bytes, total_bytes, percent)。
    无 psutil 依赖，直接读 /proc/meminfo；取不到返回 (None,None,None)。"""
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ':' in line:
                    k, v = line.split(':', 1)
                    mem[k.strip()] = int(v.split()[0])  # KB
        total = mem.get("MemTotal", 0) * 1024
        avail = mem.get("MemAvailable", 0) * 1024
        if total <= 0:
            return None, None, None
        used = total - avail
        return used, total, round(used / total * 100, 1)
    except Exception:
        return None, None, None


def _sample_gpu_live():
    idents = _gpu_ident_list()
    res = []
    for g in idents:
        temp = None; util = None; util_avail = False; util_proxy = False
        render = None; video = None; freq_mhz = None; power_w = None; rc6 = None
        mem_used = None; mem_total = None; mem_pct = None
        try:
            if g["vendor"] == "10de":  # NVIDIA
                s = run_cmd(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.memory,temperature.gpu,memory.used,memory.total",
                             "--format=csv,noheader,nounits"], 3)
                parts = [x.strip() for x in s.split(",")]
                if len(parts) >= 2:
                    try:
                        util = float(parts[0]); util_avail = True
                    except Exception:
                        pass
                    try:
                        mem_pct = float(parts[1])
                    except Exception:
                        pass
                    try:
                        temp = float(parts[2])
                    except Exception:
                        pass
                    try:
                        mem_used = int(parts[3]) * 1024 * 1024  # MiB -> bytes
                        mem_total = int(parts[4]) * 1024 * 1024
                    except Exception:
                        pass
            elif g["vendor"] == "1002":  # AMD
                b = _amd_gpu_busy(g["pci"])
                if b is not None:
                    util = b; util_avail = True
                t = _gpu_temp_from_sysfs_live(g["pci"])
                if t is not None:
                    temp = t
                # AMDGPU VRAM: /sys/class/drm/cardN/device/mem_info_vram_{used,total}
                try:
                    base = (g["pci"] or "").strip()
                    for name in os.listdir("/sys/class/drm"):
                        if not re.match(r"^card\d+$", name):
                            continue
                        devdir = os.path.join("/sys/class/drm", name, "device")
                        uevent = os.path.join(devdir, "uevent")
                        if not os.path.exists(uevent):
                            continue
                        data = open(uevent).read()
                        mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
                        if not mm:
                            continue
                        dev = mm.group(1)
                        if base and not (base == dev or dev.endswith(base) or base.endswith(dev)):
                            continue
                        used_path = os.path.join(devdir, "mem_info_vram_used")
                        total_path = os.path.join(devdir, "mem_info_vram_total")
                        if os.path.exists(used_path) and os.path.exists(total_path):
                            mem_used = int(open(used_path).read().strip())
                            mem_total = int(open(total_path).read().strip())
                            if mem_total > 0:
                                mem_pct = round(mem_used / mem_total * 100, 1)
                            break
                except Exception:
                    pass
            else:  # Intel 核显 / 无核显
                temp = _temp_snapshot_read().get("cpu_temp")
                sample = _intel_igpu_top_sample()
                if sample.get("util") is not None:
                    util = sample["util"]; util_avail = True; util_proxy = False
                if sample.get("render") is not None:
                    render = sample["render"]
                if sample.get("video") is not None:
                    video = sample["video"]
                if sample.get("freq_mhz") is not None:
                    freq_mhz = sample["freq_mhz"]
                if sample.get("power_w") is not None:
                    power_w = sample["power_w"]
                if sample.get("rc6") is not None:
                    rc6 = sample["rc6"]
                if util is None:
                    u, u_proxy = _intel_igpu_util()
                    if u is not None:
                        util = u; util_avail = True; util_proxy = u_proxy
                # 核显共享系统内存
                mu, mt, mp = _gpu_memory_bytes()
                if mt:
                    mem_used, mem_total, mem_pct = mu, mt, mp
        except Exception as _e:
            pass
        res.append({"vendor": g["vendor"], "type": g["type"], "name": g["name"], "pci": g["pci"],
                    "name_full": g.get("name_full", g["name"]), "name_arch": g.get("name_arch", ""),
                    "temp": (round(temp, 1) if isinstance(temp, (int, float)) else None),
                    "util": (round(util, 1) if isinstance(util, (int, float)) else None),
                    "render": (round(render, 1) if isinstance(render, (int, float)) else None),
                    "video": (round(video, 1) if isinstance(video, (int, float)) else None),
                    "freq_mhz": (round(freq_mhz, 1) if isinstance(freq_mhz, (int, float)) else None),
                    "power_w": (round(power_w, 2) if isinstance(power_w, (int, float)) else None),
                    "rc6": (round(rc6, 1) if isinstance(rc6, (int, float)) else None),
                    "mem_used": mem_used, "mem_total": mem_total, "mem_pct": mem_pct,
                    "util_avail": util_avail, "util_proxy": util_proxy})
    return res

def _get_gpu_live():
    now = time.time()
    if now - _GPU_LIVE_CACHE["t"] < 2:
        return _GPU_LIVE_CACHE["data"]
    try:
        data = _sample_gpu_live()
    except Exception:
        data = _GPU_LIVE_CACHE["data"]
    _GPU_LIVE_CACHE["t"] = now
    _GPU_LIVE_CACHE["data"] = data
    return data

# 启动即后台预热重型采集缓存（storcli/smartctl/docker 等同步命令耗时长）。
# 首个用户请求直接命中缓存，首屏 /api/all 从 4~5s 降至 <0.05s，不再阻塞转圈。
def _warmup_caches():
    for fn in (get_system, get_board, get_memory_modules,
               get_raid_card, get_disks, get_storage, get_docker):
        try:
            fn()
        except Exception:
            pass
    # 预热 CPU 使用率基准，避免首屏 /api/all 首次 get_cpu_usage 返回 None。
    # 直接走 get_cpu_usage()：首次会做 1s 窗口采样并写进 _CPU_USAGE_CACHE，同时给 daemon 留好 prev 基准。
    try:
        get_cpu_usage()
    except Exception:
        pass
_warmup_thread = _threading.Thread(target=_warmup_caches, daemon=True, name="cache-warmup")
_warmup_thread.start()

def get_realtime_metrics():
    with _METRICS_LOCK:
        return {
            "net": _metrics_cur["net"],
            "disk": _metrics_cur["disk"],
            "cpu_power_w": _metrics_cur["cpu_power_w"],
            "cpu_power_valid": _metrics_cur["cpu_power_valid"],
        }

def _enrich_disk_channels(disks, raid):
    """给每块盘标注通道来源：序列号命中阵列卡 storcli 盘 → 阵列卡通道；否则按接口标主板通道。"""
    raid_sn = {}
    if raid and isinstance(raid, dict) and raid.get("mode") == "mega":
        for dv in raid.get("drives", []):
            sn = (dv.get("sn") or "").strip().upper()
            if sn:
                raid_sn[sn] = dv.get("slot", "")
    locate_ok = bool(raid.get("locate_supported")) if raid else False
    for d in disks:
        sn = (d.get("serial") or "").strip().upper()
        if sn and sn in raid_sn:
            d["channel"] = f"阵列卡通道 c0:{raid_sn[sn]}"
            d["channel_type"] = "raid"
            # 把阵列卡 slot 与定位支持状态挂到盘上，供前端「物理硬盘信息」卡直接显示定位按钮
            d["slot"] = raid_sn[sn]
            d["locate_supported"] = locate_ok
        else:
            t = (d.get("type") or "").lower()
            if t == "nvme":
                d["channel"] = "主板 M.2"
                d["channel_type"] = "mobo_nvme"
            elif t == "sas":
                d["channel"] = "主板 SAS 直连"
                d["channel_type"] = "mobo_sas"
            else:
                d["channel"] = "主板 SATA 直连"
                d["channel_type"] = "mobo_sata"
    return disks


@app.route("/api/all")
def api_all():
    t0 = time.time()
    force = request.args.get("force") == "1"
    from concurrent.futures import ThreadPoolExecutor
    try:
        def _safe(fn, default):
            try:
                return fn()
            except Exception:
                return default
        # 各板块采集相互独立，并行跑，首屏不再被串行累加拖慢
        # （get_system/get_disks 有统一 TTL 缓存，force=1 时手动刷新强制重采）
        with ThreadPoolExecutor(max_workers=8) as ex:
            f_sys = ex.submit(get_system, force)
            f_board = ex.submit(get_board)
            f_mem = ex.submit(get_memory_modules)
            f_raid = ex.submit(get_raid_card)
            f_disks = ex.submit(get_disks, force)
            f_storage = ex.submit(get_storage)
            f_docker = ex.submit(get_docker)
            try:
                board = f_board.result()
            except Exception:
                board = {"manufacturer": "", "product": "", "version": ""}
            try:
                memory_modules = f_mem.result()
            except Exception:
                memory_modules = {"modules": [], "total_gb": 0, "slots": 0, "brand_summary": ""}
            try:
                system = f_sys.result()
                # 首屏直接拿真实 CPU 使用率；_warmup_caches 已预热基准，不再短 sleep。
                try:
                    system["cpu_usage"] = get_cpu_usage()
                except Exception:
                    system["cpu_usage"] = None
            except Exception:
                system = {}
            result = {
                "raid": _safe(f_raid.result, []),
                "disks": _safe(f_disks.result, []),
                "system": {**system, "board": board, "memory_modules": memory_modules},
                "storage": _safe(f_storage.result, {}),
                "docker": _safe(f_docker.result, {}),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed": round(time.time() - t0, 2),
                "nasdash_version": APP_VERSION,
                "fnos_version": _fnos_version(),
            }
            # 把阵列卡芯片温度并入 system，供「温度监控」tab 直接读取（随 /api/all /api/system 刷新）
            _raid = result.get("raid")
            if isinstance(_raid, dict):
                result["system"]["raid_temp"] = _raid.get("controller_temp")
        try:
            # 给每块盘标注通道来源（阵列卡 / 主板），供前端「物理硬盘信息」区分
            result["disks"] = _enrich_disk_channels(result.get("disks", []) or [], result.get("raid") or {})
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

# ===================== 按板块独立接口（切换导航只拉当前板块，避免全量 /api/all）=====================
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
        system = {**get_system(request.args.get("force") == "1"), "board": board, "memory_modules": memory_modules}
        try:
            system["cpu_usage"] = get_cpu_usage()
        except Exception:
            system["cpu_usage"] = None
        # 阵列卡芯片温度（ROC Temperature），供「温度监控」tab 显示（get_raid_card 有 60s 缓存，开销极低）
        try:
            _raid = get_raid_card()
            if isinstance(_raid, dict):
                system["raid_temp"] = _raid.get("controller_temp")
        except Exception:
            system["raid_temp"] = None
        return jsonify({"system": system, "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/raid")
def api_raid():
    """阵列卡板块。#raid 渲染同时依赖 raid + disks，故一并返回（raid 带 60s 缓存，disks 带 300s 缓存）。"""
    t0 = time.time()
    try:
        return jsonify({"raid": get_raid_card(), "disks": get_disks(),
                        "cc_schedule": _load_cc_schedule(),
                        "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/raid/locate", methods=["POST"])
def api_raid_locate():
    """物理盘定位（locate LED 闪烁）。仅当控制器/背板支持时可用（locate_supported）。"""
    try:
        body = request.get_json(force=True, silent=True) or {}
        slot = str(body.get("slot", "")).strip()
        action = str(body.get("action", "")).strip().lower()
        if not re.match(r"^\d+:\d+$", slot):
            return jsonify({"ok": False, "error": "invalid slot"})
        if action not in ("start", "stop"):
            return jsonify({"ok": False, "error": "invalid action"})
        rc = get_raid_card()
        if not rc.get("locate_supported"):
            return jsonify({"ok": False, "error": "本机阵列卡/背板不支持定位闪灯"})
        valid = {d.get("slot") for d in (rc.get("drives") or [])}
        if slot not in valid:
            return jsonify({"ok": False, "error": "slot 不在阵列卡接管盘列表中"})
        e, s = slot.split(":")
        verb = "start" if action == "start" else "stop"
        out = sudo_cmd([STORCLI, "/c0", "/e" + e, "/s" + s, verb, "locate"], 20)
        ok = bool(out and "Succeeded" in out)
        return jsonify({"ok": ok, "action": action, "slot": slot, "out": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})

def _raid_cc_vnum(vd_arg, vds):
    """把前端传入的 vd 参数（纯数字或 dgvd 如 0/0）解析为 storcli 可用的 vX 序号，并校验存在。"""
    if vd_arg is None:
        return None
    vd_arg = str(vd_arg).strip()
    vnum = vd_arg.split("/")[1] if re.match(r"^\d+/\d+$", vd_arg) else vd_arg
    if not re.match(r"^\d+$", vnum):
        return None
    if 0 <= int(vnum) < len(vds):
        return vnum
    valid = set()
    for v in vds:
        dgvd = v.get("dgvd", "")
        if re.match(r"^\d+/\d+$", dgvd):
            valid.add(dgvd.split("/")[1])
    return vnum if vnum in valid else None

@app.route("/api/raid/cc", methods=["GET", "POST"])
def api_raid_cc():
    """阵列卡一致性检查（Consistency Check）。

    仅对存在的硬件 RAID 逻辑盘（VD）执行；本机为 JBOD 直通时无 VD，接口直接返回明确错误（不崩溃）。
    - GET  ?vd=0         ：查询该 VD 的 CC 进度与状态（idle/running/paused + 百分比）
    - POST {vd, action}  ：action = start|pause|resume|stop（stop 后不可恢复）
    CC 为只读扫描（不改数据），但会抬升磁盘负载，前端在开始前需二次确认。
    """
    try:
        rc = get_raid_card()
        vds = rc.get("virtual_drives") or []
        if not vds:
            return jsonify({"ok": False,
                            "error": "本机无硬件 RAID 逻辑盘（VD），无法执行一致性检查（当前为 JBOD 直通模式，阵列由系统层 mdadm 管理）"})
        if request.method == "GET":
            vnum = _raid_cc_vnum(request.args.get("vd"), vds)
            if vnum is None:
                return jsonify({"ok": False, "error": "invalid vd"})
            out = sudo_cmd([STORCLI, "/c0", "/v" + vnum, "show", "cc"], 15) or ""
            prog = re.search(r"Progress\s*=?\s*(\d+)%", out)
            pct = int(prog.group(1)) if prog else None
            state = "idle"
            if re.search(r"running|In progress|Active", out, re.I):
                state = "running"
            elif re.search(r"paused|suspended", out, re.I):
                state = "paused"
            return jsonify({"ok": True, "vd": vnum, "state": state, "progress": pct, "raw": out})
        body = request.get_json(force=True, silent=True) or {}
        vnum = _raid_cc_vnum(body.get("vd"), vds)
        if vnum is None:
            return jsonify({"ok": False, "error": "invalid vd"})
        action = str(body.get("action", "")).strip().lower()
        if action not in ("start", "pause", "resume", "stop"):
            return jsonify({"ok": False, "error": "invalid action (start|pause|resume|stop)"})
        force = bool(body.get("force", False))
        cmd = [STORCLI, "/c0", "/v" + vnum, action, "cc"]
        if action == "start" and force:
            cmd.append("force")
        out = sudo_cmd(cmd, 30) or ""
        ok = ("Succeeded" in out) or ("Status = Success" in out)
        return jsonify({"ok": ok, "vd": vnum, "action": action, "out": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})


# ===================== 一致性检查定时调度 =====================
import threading  # 后台线程触发 CC（模块级重复导入无害）


_CC_SCHEDULE_FILE = os.path.join(_config_dir(), "cc_schedule.json")
_cc_sched_last_check = 0


def _load_cc_schedule():
    """读取一致性检查定时调度配置（JSON），缺失字段补默认值。"""
    try:
        with open(_CC_SCHEDULE_FILE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("period", "daily")      # daily | weekly
    d.setdefault("hour", 3)
    d.setdefault("minute", 0)
    d.setdefault("weekday", 0)           # 0=周一 … 6=周日（仅 weekly 用）
    d.setdefault("vd", "all")            # all | 特定 dgvd 如 "0/0"
    d.setdefault("last_run", None)       # 上次触发的周期键，防重复触发
    return d


def _save_cc_schedule(cfg):
    try:
        with open(_CC_SCHEDULE_FILE, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _check_cc_schedule():
    """由 metrics_collect_loop 周期调用：到点则对硬件逻辑盘触发一致性检查。
    无 VD（如 JBOD 直通）或配置未启用时静默返回，绝不抛异常。"""
    try:
        cfg = _load_cc_schedule()
        if not cfg.get("enabled"):
            return
        now = time.localtime()
        if int(cfg.get("hour", 3)) != now.tm_hour or int(cfg.get("minute", 0)) != now.tm_min:
            return
        if cfg.get("period") == "weekly" and int(cfg.get("weekday", 0)) != now.tm_wday:
            return
        cycle_key = time.strftime("%Y-W%W") if cfg.get("period") == "weekly" else time.strftime("%Y-%m-%d")
        if cfg.get("last_run") == cycle_key:
            return  # 本周期已触发
        try:
            rc = get_raid_card()
            vds = rc.get("virtual_drives") or []
        except Exception:
            vds = []
        if not vds:
            # 无 VD（JBOD 直通等）：更新 last_run 避免反复尝试，不报错
            cfg["last_run"] = cycle_key
            _save_cc_schedule(cfg)
            return
        targets = vds
        sel = cfg.get("vd")
        if sel not in ("all", None, ""):
            targets = [v for v in vds if v.get("dgvd") == sel]
        for v in targets:
            vnum = _raid_cc_vnum(v.get("dgvd") or "0/0", vds)
            if vnum is None:
                continue
            try:
                sudo_cmd([STORCLI, "/c0", "/v" + vnum, "start", "cc"], 30)
            except Exception:
                pass
        cfg["last_run"] = cycle_key
        _save_cc_schedule(cfg)
    except Exception:
        pass


@app.route("/api/raid/cc/schedule", methods=["GET", "POST"])
def api_raid_cc_schedule():
    """一致性检查定时调度配置。
    GET：返回当前配置；POST {enabled, period, hour, minute, weekday, vd}：保存。
    配置本身与是否存在 VD 无关（保存合法）；真正触发由 _check_cc_schedule 按时执行，
    无 VD 时静默跳过。前端仅在存在硬件逻辑盘时展示该配置 UI。"""
    if request.method == "GET":
        return jsonify(_load_cc_schedule())
    try:
        body = request.get_json(force=True, silent=True) or {}
        cfg = _load_cc_schedule()
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
        if "period" in body:
            cfg["period"] = "weekly" if str(body["period"]) == "weekly" else "daily"
        if "hour" in body:
            cfg["hour"] = max(0, min(23, int(body["hour"])))
        if "minute" in body:
            cfg["minute"] = max(0, min(59, int(body["minute"])))
        if "weekday" in body:
            cfg["weekday"] = max(0, min(6, int(body["weekday"])))
        if "vd" in body:
            cfg["vd"] = body["vd"]
        _save_cc_schedule(cfg)
        return jsonify({"ok": True, "config": cfg})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})


@app.route("/api/raid/hotspare", methods=["GET", "POST"])
def api_raid_hotspare():
    """阵列卡热备盘分配（全局 GHS / 专用 DHS）。
    仅对存在的硬件 RAID 环境有效；本机 JBOD 直通无 VD 时接口返回明确错误（不崩溃）。
    - GET            ：返回当前热备盘列表（含类型 global/dedicated）
    - POST {slot, action}：action = add(全局) | add_dedicated(专用) | remove
    """
    try:
        rc = get_raid_card()
        if rc.get("mode") != "mega":
            return jsonify({"ok": False, "error": "当前非 MegaRAID 模式，无法管理热备盘"})
        if request.method == "GET":
            return jsonify({"ok": True, "hotspares": rc.get("hotspares", [])})
        body = request.get_json(force=True, silent=True) or {}
        slot = str(body.get("slot", "")).strip()
        m = re.match(r"^(\d+):(\d+)$", slot)
        if not m:
            return jsonify({"ok": False, "error": "invalid slot (需形如 252:0)"})
        e, s = m.group(1), m.group(2)
        action = str(body.get("action", "")).strip().lower()
        if action not in ("add", "add_dedicated", "remove"):
            return jsonify({"ok": False, "error": "invalid action (add|add_dedicated|remove)"})
        if action == "remove":
            cmd = [STORCLI, "/c0", "/e" + e, "/s" + s, "delete", "hotsparedrive"]
        else:
            cmd = [STORCLI, "/c0", "/e" + e, "/s" + s, "add", "hotsparedrive"]
            if action == "add_dedicated":
                cmd.append("dedicated")
        out = sudo_cmd(cmd, 30) or ""
        ok = ("Succeeded" in out) or ("Status = Success" in out)
        return jsonify({"ok": ok, "action": action, "slot": slot, "out": out})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})


@app.route("/api/raid/copyback", methods=["GET", "POST"])
def api_raid_copyback():
    """阵列卡 CopyBack 自动换盘（监控 + 触发）。
    仅对硬件 RAID（存在 VD）环境有效；本机 JBOD 直通无 VD 时接口返回明确错误（不崩溃）。
    - GET                  ：返回自动 CopyBack 开关状态 + 当前正在/待换盘的盘
    - POST {action}        ：action = enable | disable（开/关控制器自动 CopyBack）
    - POST {slot, action}  ：action = start（手动对故障盘触发 CopyBack，须先配热备盘）
    """
    try:
        rc = get_raid_card()
        if rc.get("mode") != "mega":
            return jsonify({"ok": False, "error": "当前非 MegaRAID 模式，无 CopyBack 能力"})
        if not rc.get("virtual_drives"):
            return jsonify({"ok": False, "error": "当前无硬件 RAID 逻辑盘（VD），CopyBack 仅在已配置热备盘的阵列环境下生效"})
        if request.method == "GET":
            return jsonify({
                "ok": True,
                "auto_copyback": rc.get("auto_copyback", "unknown"),
                "copyback_active": [d["slot"] for d in rc.get("drives", []) if d.get("copyback_active")],
                "copyback_needed": rc.get("copyback_needed", []),
            })
        body = request.get_json(force=True, silent=True) or {}
        action = str(body.get("action", "")).strip().lower()
        if action in ("enable", "disable"):
            val = "on" if action == "enable" else "off"
            out = sudo_cmd([STORCLI, "/c0", "set", "copyback=" + val], 30) or ""
            ok = ("Succeeded" in out) or ("Status = Success" in out)
            return jsonify({"ok": ok, "action": action, "auto_copyback": val, "out": out})
        if action == "start":
            slot = str(body.get("slot", "")).strip()
            m = re.match(r"^(\d+):(\d+)$", slot)
            if not m:
                return jsonify({"ok": False, "error": "invalid slot (需形如 252:0)"})
            e, s = m.group(1), m.group(2)
            out = sudo_cmd([STORCLI, "/c0", "/e" + e, "/s" + s, "start", "copyback"], 30) or ""
            ok = ("Succeeded" in out) or ("Status = Success" in out)
            return jsonify({"ok": ok, "action": "start", "slot": slot, "out": out})
        return jsonify({"ok": False, "error": "invalid action (enable|disable|start)"})
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)})


@app.route("/api/disks")
def api_disks():
    """硬盘 SMART 板块。

    standalone（是否独立盘，决定能否跑破坏性 badblocks）必须实时计算，
    不能跟着 get_disks 的 300s 缓存走——否则用户刚从阵列移除硬盘，
    B 档按钮要等 5 分钟缓存过期才变绿。SMART 等重数据仍走缓存。
    """
    t0 = time.time()
    try:
        cached = get_disks(request.args.get("force") == "1")
        disks = []
        for d in cached:
            d2 = dict(d)
            ok, reason = _is_standalone_disk(d.get("dev"))
            d2["standalone"], d2["standalone_reason"] = ok, reason
            disks.append(d2)
        # 标注通道来源（阵列卡 / 主板），供前端「物理硬盘信息」区分
        try:
            raid = get_raid_card()
        except Exception:
            raid = {}
        disks = _enrich_disk_channels(disks, raid)
        return jsonify({"disks": disks, "time": _panel_time(), "elapsed": round(time.time() - t0, 2)})
    except Exception as e:
        return jsonify({"error": str(e), "time": _panel_time()})

@app.route("/api/disks/selftest", methods=["GET"])
def api_disks_selftest_get():
    """返回当前硬盘自检任务状态（只读）。"""
    with DISK_TEST_LOCK:
        jobs = {}
        for k, v in DISK_TEST_JOBS.items():
            jobs[k] = {kk: vv for kk, vv in v.items() if kk not in ("pid", "_proc", "outfile")}
    return jsonify({"ok": True, "jobs": jobs})


@app.route("/api/disks/selftest/history", methods=["GET"])
def api_disks_selftest_history():
    """返回硬盘自检历史记录（新→旧，最多 50 条）。"""
    hist = _load_disk_test_history()
    hist = list(reversed(hist))
    dev = (request.args.get("dev") or "").strip()
    if dev:
        hist = [r for r in hist if r.get("dev") == dev]
    return jsonify({"ok": True, "history": hist})


@app.route("/api/disks/selftest", methods=["POST"])
@require_admin()
def api_disks_selftest_post():
    """启动硬盘自检：type=long（SMART 长自检）/ badblocks（破坏性，仅独立盘）/ surface（只读表面扫描，所有盘）。"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    dev = data.get("dev", "")
    ttype = data.get("type", "")
    confirm = data.get("confirm", False)
    if not isinstance(dev, str) or not _safe_token(dev):
        return jsonify({"ok": False, "error": "非法设备名"}), 400
    if ttype not in ("long", "badblocks", "surface"):
        return jsonify({"ok": False, "error": "type 须为 long / badblocks / surface"}), 400
    disks = get_disks()
    if not any(d.get("dev") == dev for d in disks):
        return jsonify({"ok": False, "error": "未找到该硬盘"}), 400
    with DISK_TEST_LOCK:
        if dev in DISK_TEST_JOBS and DISK_TEST_JOBS[dev]["state"] == "running":
            return jsonify({"ok": False, "error": "该硬盘已有正在进行的自检"}), 400
    if ttype == "badblocks":
        if not confirm:
            return jsonify({"ok": False, "error": "B 类坏块慢扫为破坏性测试，请确认"}), 400
        ok, reason = _is_standalone_disk(dev)
        if not ok:
            return jsonify({"ok": False, "error": reason}), 400
    if ttype == "surface":
        if not confirm:
            return jsonify({"ok": False, "error": "C 类表面扫描耗时较长，请确认"}), 400
    try:
        if ttype == "long":
            _start_smart_long(dev)
        elif ttype == "surface":
            _start_badblocks(dev, mode="surface")
        else:
            _start_badblocks(dev, mode="destructive")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/disks/selftest/abort", methods=["POST"])
@require_admin()
def api_disks_selftest_abort():
    """中止指定硬盘的自检任务。"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    dev = data.get("dev", "")
    if not isinstance(dev, str) or not _safe_token(dev):
        return jsonify({"ok": False, "error": "非法设备名"}), 400
    with DISK_TEST_LOCK:
        job = DISK_TEST_JOBS.get(dev)
    if not job:
        return jsonify({"ok": False, "error": "无进行中的自检"}), 400
    if job["state"] != "running":
        return jsonify({"ok": False, "error": "该硬盘自检已结束"}), 400
    try:
        if job["type"] == "long":
            _abort_smart_long(dev)
        else:
            _abort_badblocks(dev)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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

def get_live_mem():
    """实时内存占用率(%):读 /proc/meminfo 用 MemAvailable 估算(无 psutil 依赖)。"""
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ':' in line:
                    k, v = line.split(':', 1)
                    mem[k.strip()] = int(v.split()[0])  # KB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total <= 0:
            return None
        return round((1 - avail / total) * 100, 1)
    except Exception:
        return None

@app.route("/api/network")
def api_network():
    """独立网卡接口：返回 get_network_nics() 结果（IP/MAC/速率/驱动/状态等）。
    不进 get_system 的 60s TTL 缓存，供前端「网络」卡片轻量(5s)刷新，
    避免被 RAID/SMART/Docker 等慢采集拖慢。"""
    try:
        nics = get_network_nics()
        resp = jsonify({"nics": nics, "time": _panel_time()})
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp
    except Exception as e:
        return jsonify({"nics": [], "error": str(e)}), 500


# 磁盘静态元数据缓存：/api/metrics 被前端每 1s 轮询，而 get_disks() 对每块盘跑 smartctl
# （每块 20s 超时）只为取 model/size/brand/type 这类几乎不变的静态信息。
# 无缓存时每秒全量扫盘（与 /api/fan/temps 同源风暴），把磁盘 I/O 打满并拖慢接口（实测 ~2s）。
# 元数据 5 分钟不变，TTL 300s；health/temp 等动态字段由磁盘页各自实时读取，不受影响。
# 加锁防并发双扫：过期时非阻塞抢锁，抢不到就返回旧值，绝不排队等待。
_DISKS_META_CACHE = {"t": 0.0, "v": None}
_DISKS_META_TTL = 300
_DISKS_META_LOCK = _threading.Lock()

def _disks_meta_map():
    now = time.time()
    c = _DISKS_META_CACHE
    if now - c["t"] <= _DISKS_META_TTL and c["v"] is not None:
        return c["v"]
    if _DISKS_META_LOCK.acquire(blocking=False):
        try:
            if now - c["t"] > _DISKS_META_TTL:
                m = {}
                for d in get_disks():
                    m[d.get("dev")] = d
                c["v"] = m
                c["t"] = time.time()
        except Exception:
            pass
        finally:
            _DISKS_META_LOCK.release()
    return c["v"] or {}

@app.route("/api/metrics")
def api_metrics():
    """轻量实时指标：网络吞吐 + 磁盘 I/O + CPU/内存/负载。供前端高频(1s)轮询，不触发重型 /api/all(阵列卡/SMART等)。
    网络速率会把 OVS 桥（如 eno1-ovs）归并到物理口名（eno1），与 /api/system 的合并显示名对齐，
    这样前端按 data-nic 直接覆盖即可。"""
    try:
        rt = get_realtime_metrics()
        net = rt.get("net", [])
        merged = {}
        for e in net:
            name = e.get("name", "")
            if name.endswith("-ovs"):
                base = name[:-4]
                merged[base] = {"name": base,
                                "rx_rate": e.get("rx_rate", 0.0),
                                "tx_rate": e.get("tx_rate", 0.0)}
            elif name not in merged:
                merged[name] = {"name": name,
                                "rx_rate": e.get("rx_rate", 0.0),
                                "tx_rate": e.get("tx_rate", 0.0)}
        diskio = rt["disk"]
        try:
            disk_map = _disks_meta_map()
        except Exception:
            disk_map = {}
        for d in diskio:
            info = disk_map.get(d["device"], {})
            d["model"] = info.get("model", "")
            d["size"] = info.get("size", "")
            d["brand"] = info.get("brand", "")
            d["type"] = info.get("type", "")
        # 实时 CPU / 内存 / 负载（无缓存，供前端 1s 增量刷新）。
        # CPU 走 get_cpu_usage 的 1s 窗口缓存（daemon 每 1s 刷新；异常时就地 1s 采样兜底），
        # 采样窗口恒为 ~1s，与 fnOS 口径一致，不再有毫秒级窗口导致的瞬时 100%/50% 尖刺。
        try:
            cpu_usage = get_cpu_usage()
        except Exception:
            cpu_usage = None
        try:
            mem_percent = get_live_mem()
        except Exception:
            mem_percent = None
        try:
            load = list(os.getloadavg())
        except Exception:
            load = None
        return jsonify({"net": list(merged.values()), "diskio": diskio,
                        "cpu_usage": cpu_usage, "mem_percent": mem_percent, "load": load,
                        "gpu": _get_gpu_live(),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    except Exception as e:
        return jsonify({"error": str(e), "net": [], "diskio": []})

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
        if not _FAN_CTRL_ENABLED:
            return jsonify({"ok": False, "error": "风扇接管已关闭，nasdash 不控速（仅监控）"}), 400
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
    # 别名组：同一物理风扇多通道（用户为这些通道设了相同名称）→ 控速同步到全部成员
    _keys = _fan_alias_members_of(hwmon, idx)
    if mode == "auto":
        ext = _fan_ext_service_running()
        for (h, i) in _keys:
            if ext:
                # 系统风扇服务在跑：交还它接管（写 enable=2）
                try:
                    with open(f"{h}/pwm{i}_enable", "w") as f:
                        f.write("2")
                except Exception:
                    pass
                with FAN_LOCK:
                    FAN_TARGETS.pop((h, i), None)
            else:
                # 系统风扇服务未运行：nasdash 自带保守温控曲线接管
                with FAN_LOCK:
                    FAN_TARGETS[(h, i)] = {"mode": "auto", "target": None}
            _save_fan_mode(i, "auto", None)
        owner = "ext_service" if ext else "nasdash"
        return jsonify({"ok": True, "mode": "auto", "owner": owner})
    try:
        pct = int(pwm)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid pwm"}), 400
    FLOOR = 0  # 允许命令到 0%（真正停转）。有用户反馈其硬件在 10% 已停转，需要能设到 0%
    pct = max(FLOOR, min(100, pct))
    raw = round(pct / 100 * 255)
    # 仅设为目标值，由缓变线程平滑过渡（不再瞬间写 255，避免突然全速）
    for (h, i) in _keys:
        with FAN_LOCK:
            FAN_TARGETS[(h, i)] = {"mode": "manual", "target": raw}
        _save_fan_mode(i, "manual", raw)
    return jsonify({"ok": True, "mode": "manual", "pwm": pct, "raw": raw, "aliased": len(_keys) > 1})


@app.route("/api/fan/pwm_mode", methods=["POST"])
@require_admin()
def api_fan_pwm_mode():
    """切换风扇接口的调速信号类型：PWM(4pin 脉宽) ↔ DC(3pin 直流电压)。

    背景：3pin 风扇插在被设成 PWM 的接口上会一直全速——因为它没有 PWM 线，只认电压。
    多数主板 BIOS 里能给每个接口单独选 PWM/DC，Linux 下对应 pwmN_mode 寄存器。
    但**能不能改由主板硬件决定**：部分接口只焊了 PWM 电路，写 DC 会被内核直接拒绝，
    这种情况只能进 BIOS 或换 4pin 风扇，属主板限制而非软件问题。
    """
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    hwmon = data.get("hwmon")
    idx = data.get("idx")
    mode = (data.get("pwm_mode") or "").strip().lower()
    if not isinstance(hwmon, str) or not hwmon.startswith("/sys/class/hwmon/hwmon"):
        return jsonify({"ok": False, "error": "invalid hwmon"}), 400
    try:
        idx = int(idx)
        if idx < 1 or idx > 10:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid idx"}), 400
    if mode not in ("pwm", "dc"):
        return jsonify({"ok": False, "error": "invalid pwm_mode"}), 400
    # 与 /api/fan/set 一致：hwmon 编号可能跨重启漂移，按实时枚举校正
    _enum = _enumerate_fans()
    if (hwmon, idx) not in _enum:
        _idx2hw = {i: h for (h, i) in _enum}
        if idx in _idx2hw:
            hwmon = _idx2hw[idx]
    cur = _fan_read_pwm_mode(hwmon, idx)
    if cur is None:
        return jsonify({"ok": False, "error": "该接口不支持切换调速方式（主板未提供此寄存器）"}), 400
    if cur == mode:
        return jsonify({"ok": True, "pwm_mode": mode, "changed": False})
    ok, err = _fan_set_pwm_mode(hwmon, idx, mode)
    if not ok:
        return jsonify({"ok": False, "error": err, "pwm_mode": cur}), 400
    return jsonify({"ok": True, "pwm_mode": mode, "changed": True})


def _fan_alias_map():
    """别名组：以『用户自定义同名标注』连接多个不同 hwmon::idx 通道（同一物理风扇被识别成两张卡）。
    返回 {key:"hwmon::idx": [member keys...]}。仅当用户为多个通道设了相同非空 name 时才成组，
    避免把恰巧同名的不同风扇误合并（论坛 huhaibo820 反馈 #1：改名/隐藏联动、两张卡合一）。"""
    labels = _load_fan_labels()
    by_name = {}
    for k, v in (labels or {}).items():
        if not isinstance(v, dict):
            continue
        name = (v.get("name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(k)
    amap = {}
    for name, keys in by_name.items():
        if len(keys) > 1:
            for k in keys:
                amap[k] = list(keys)
    return amap


def _fan_alias_members_of(hwmon, idx):
    """返回含别名的全部成员 key 列表 [(hwmon, idx), ...]（含自身）。"""
    amap = _fan_alias_map()
    ks = "%s::%d" % (hwmon, idx)
    members = amap.get(ks, [ks])
    out = []
    for m in members:
        if "::" in m:
            h, i = m.split("::", 1)
            try:
                out.append((h, int(i)))
            except (TypeError, ValueError):
                pass
    return out or [(hwmon, idx)]


def _apply_fan_aliases(fans):
    """把同名标注的多通道风扇合并为一张卡：代表取有转速的通道，改名/隐藏同步到全部成员。"""
    amap = _fan_alias_map()
    if not amap:
        return fans
    idx_of = {}
    for f in fans:
        idx_of["%s::%d" % (f["hwmon"], f["idx"])] = f
    seen = set()
    out = []
    for f in fans:
        k = "%s::%d" % (f["hwmon"], f["idx"])
        if k not in amap:
            out.append(f)
            continue
        grp = tuple(sorted(amap[k]))
        if grp in seen:
            continue  # 成员已并入代表，跳过
        seen.add(grp)
        members = [idx_of[m] for m in grp if m in idx_of]
        rep = next((m for m in members if (m.get("rpm") or 0) > 0), members[0])
        rep = dict(rep)
        rep["alias_members"] = list(grp)
        rpms = [m.get("rpm") or 0 for m in members]
        rep["rpm"] = max(rpms) if any(rpms) else (rep.get("rpm") or 0)
        rep["hidden"] = bool(any(m.get("hidden") for m in members))
        out.append(rep)
    return out


def _replicate_aliases(clean):
    """标注整体保存时，把同名组的条目同步到所有成员（改名/隐藏联动）。"""
    amap = _fan_alias_map()
    if not amap:
        return
    for ks, members in amap.items():
        entry = clean.get(ks)
        if not entry:
            continue
        for m in members:
            if m == ks:
                continue
            clean[m] = dict(entry)


def _dedup_fan_names(fans):
    """展示名去重：同名风扇补 #2/#3 后缀，便于在 UI 区分（如主板把两通道都报成 CHA_FAN1）。
    仅影响前端展示，不影响持久化 key（hwmon::idx）——改名/隐藏逻辑仍按唯一 key 工作。"""
    seen = {}
    for f in fans:
        n = f.get("name")
        if not n:
            continue
        if n in seen:
            seen[n] += 1
            f["name"] = "%s #%d" % (n, seen[n])
        else:
            seen[n] = 1
    return fans


def get_fan_status():
    """风扇实时状态列表（供前端轮询与硬件健康报告复用）。"""
    fans = []
    labels = _load_fan_labels()
    _dt = _load_fan_disk_temp()
    _rules = _effective_fan_rules()   # 逐风扇温控规则（判定每台风扇是否被温控接管 + 算目标）
    # 逐风扇温控目标：温度源每种只算一次，供状态展示（与调速线程口径一致）
    _need_disk = any((r.get("source", "disk") == "disk") for r in _rules.values())
    _need_combo = any((r.get("source") or "").startswith("combo") for r in _rules.values())
    _need_cpu = any((r.get("source") == "cpu") for r in _rules.values()) or _need_combo
    _need_mb = any((r.get("source") == "mb") for r in _rules.values()) or _need_combo
    _disk_idle_s, _disk_T, _disk_has = (False, None, False)
    if _need_disk:
        _dt_eff = _dt
        if not _dt.get("disks"):
            # 逐风扇 disk 规则不依赖全局监控盘白名单：自动用本机全部盘作温度源
            _all_devs = _list_all_disk_devs()
            if _all_devs:
                _dt_eff = dict(_dt, disks=_all_devs)
        _disk_idle_s, _disk_T, _disk_has = _disk_source_state(_dt_eff)
    _cpu_T = _fan_read_sys_temp("cpu") if _need_cpu else None
    _mb_T = _fan_read_sys_temp("mb") if _need_mb else None
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
        key = (hwmon, idx)
        # 逐风扇温控：该风扇若命中规则，模式按温度源分类（沿用旧徽标 disk_temp/sys_temp），
        # 并附带该风扇自己的规则与实时计算目标，供前端逐扇展示。
        _rule = _rules.get("%s::%d" % (hwmon, idx))
        rule_out = _rule
        # 3按钮显式控速方案：'linear'（开转/全速）/'curve'（自定义曲线）；手动时为 None
        active_mode = (_rule.get("active_mode") if _rule else None)
        computed_pwm = None
        rule_source = None
        if _rule:
            _src = _rule.get("source", "disk")
            rule_source = _src
            _rt, _ridle = _resolve_rule_temp(_src, _cpu_T, _mb_T, _disk_T, _disk_idle_s, _disk_has)
            if _rt is not None or _ridle:
                _raw = 0 if _ridle else _fan_rule_pwm(_rt, _rule)
                if _raw is not None:
                    computed_pwm = round(_raw / 255 * 100)
            # 显式分两类：手动覆盖时不算温控模式；规则存在但未接管（如曲线无效/温度低）
            # 时维持 3按钮的高亮态（active_mode 优先于 mode 标签）。
            if not active_mode:
                # 旧配置无 active_mode：回退到「linear/curve」按曲线有无判断
                active_mode = "curve" if _rule.get("curve") else "linear"
            if _src in ("cpu", "mb") or _src.startswith("combo"):
                mode = "sys_temp" if active_mode == "linear" else "curve"
            else:
                mode = "disk_temp" if active_mode == "linear" else "curve"
        else:
            # 无温控规则：以用户设的逻辑模式为准。nasdash 接管控速时硬件 pwm_enable 恒为 1（manual），
            # 不能据此判断，故显示 auto（下面若 tcfg 为 manual 会再覆盖为 manual）。
            mode = "auto"
            active_mode = None
        target_pct = None
        with FAN_LOCK:
            tcfg = FAN_TARGETS.get(key)
        if tcfg and tcfg.get("mode") == "manual":
            target_pct = round(tcfg["target"] / 255 * 100)
        # #4 修复：用户显式手动调速时手动优先于温度联动，状态标签显示「手动控制」（与控速行为一致）
        if tcfg and tcfg.get("mode") == "manual":
            mode = "manual"
            active_mode = None
        _lbl = labels.get(f"{hwmon}::{idx}", {})
        fans.append({
            "name": _lbl.get("name") or names.get(idx, f"风扇{idx}"),
            "label": _lbl.get("name", ""),
            "voltage": _lbl.get("voltage", ""),
            "idx": idx, "hwmon": hwmon,
            "rpm": rpm, "pwm": pwm_pct,
            "pwm_enable": _pe,   # 原始寄存器值：0=关闭 1=软件控(手动) 2=交还主板/内核自动
            # 调速信号类型：'pwm'=4pin 脉宽调速 / 'dc'=3pin 直流电压调速 / None=该通道无此寄存器。
            # DC 模式下 nasdash 写的占空比按电压比例输出，低档更易停转，前端会给出提示。
            "pwm_mode": _fan_read_pwm_mode(hwmon, idx),
            "mode": mode,
            "rule": rule_out,
            "rule_source": rule_source,
            "computed_pwm": computed_pwm,
            "target_pct": target_pct,
            "controllable": True,
            # has_tach=False：该通道读不到转速（分线器副扇/未接转速线/主板未布线该通道）
            "has_tach": rpm > 0,
            # no_tach=True：用户已标注「此口风扇无转速反馈线（2/3针）」，读不到转速属正常。
            # 与 pwm_mode=='pwm' 同时成立时，前端提示占空比很可能不起作用（无信号线→恒满速）。
            "no_tach": bool(_lbl.get("no_tach")),
            # 用户可把「无风扇的幽灵通道」隐藏（持久化到 fan_labels.json）
            "hidden": bool(_lbl.get("hidden")),
            # 手动/曲线共存模型：曲线(rule)与手动覆盖相互独立，互不销毁（论坛 #3 反馈）。
            # has_curve=是否存了曲线；manual_active=当前是否处于手动态（曲线被临时盖住）。
            "has_curve": _rule is not None,
            "manual_active": bool(tcfg and tcfg.get("mode") == "manual"),
            # 3按钮显式方案：'linear'/'curve'/None(手动)
            "active_mode": active_mode,
        })
    fans = _apply_fan_aliases(fans)
    return _dedup_fan_names(fans)

@app.route("/api/fan/status")
def api_fan_status():
    """轻量风扇状态：供前端高频轮询，实时显示转速/当前占空比/目标（常驻线程 2s tick）"""
    return jsonify({"fans": get_fan_status(), "control_enabled": _FAN_CTRL_ENABLED})


@app.route("/api/fan/control")
def api_fan_control_get():
    """风扇接管总开关状态。"""
    return jsonify({"enabled": _FAN_CTRL_ENABLED})

@app.route("/api/fan/control", methods=["POST"])
@require_admin()
def api_fan_control_set():
    """开关风扇接管：关闭后 nasdash 不再根据温度自动调速。若飞牛自带风扇服务已配置，
    会交还它接管；否则保持当前转速、不再写 PWM。"""
    global _FAN_CTRL_ENABLED
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    enabled = bool(data.get("enabled", True))
    if not _save_json_file(FAN_CONTROL_CFG, {"enabled": enabled}):
        return jsonify({"ok": False, "error": "写入配置失败"}), 500
    _FAN_CTRL_ENABLED = enabled
    # 状态已切换，立即清掉 FCS 状态缓存，让前端下一次拉取拿到最新状态。
    _fcs_status_cached(clear=True)
    if enabled:
        # 重新接管前必须停掉系统风扇服务，否则 nasdash 会因 FCS 在跑而拒绝写 PWM。
        # 只有 FCS 真的配置了参数时才需要停；没配置时它本来就不会控速。
        # systemctl stop 交给后台线程，避免阻塞本 API 响应（开关才能真正秒回）。
        try:
            if _fcs_has_board_config():
                def _stop_fcs():
                    try:
                        _fan_stop_ext_service()
                        _FCS_TAKEN["v"] = True
                    except Exception:
                        pass
                _threading.Thread(target=_stop_fcs, daemon=True).start()
        except Exception:
            pass
        _FAN_ENABLE_CACHE.clear()
    else:
        # 接管关闭：后台线程把风扇降到安全待机转速并停手（详见 _fan_release_to_idle 注释），
        # 避免同步写 sysfs 阻塞 API，造成前端 toggle 卡住、用户感觉「关不掉/打不开」。
        def _release():
            try:
                _fan_release_to_idle()
            except Exception:
                pass
        _threading.Thread(target=_release, daemon=True).start()
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/fan/temps")
def api_fan_temps():
    """温度统一快照：CPU/主板/各硬盘/全量测点/阵列卡，供前端「当前温度」卡与温度墙高频展示。
    全部数据来自后台采集循环的 _TEMP_SNAP（~2s 刷新），不再各自采样。"""
    _snap = _temp_snapshot_read()
    cpu_T = _snap.get("cpu_temp")
    mb_T = _snap.get("mb_temp")
    states = _snap.get("disks") or {}
    devs = list(states.keys())
    disks = []
    for d in devs:
        st = states.get(d, {})
        fi = _disk_friendly_info(d)
        disks.append({
            "dev": d,
            "name": fi["name"],
            "model": fi["model"],
            "category": fi["category"],
            "is_system": fi["is_system"],
            "temp": st.get("temp"),
            "asleep": st.get("asleep"),
            "no_sleep": st.get("no_sleep", False),
            # 供前端标注「被动散热、不参与风扇温控」，并说明其判定阈值与机械盘不同
            "is_nvme": st.get("is_nvme", False),
            "nvme_start_temp": NVME_START_TEMP,
        })
    # 同型号同容量的盘（典型：双磁臂 SAS 盘拆成两个 LUN、或买了两块一样的盘）友好名会撞车，
    # 撞车时补上内核名后缀，保证「一眼能认出是哪块」这个目标不被重名破坏。
    _name_count = {}
    for it in disks:
        _name_count[it["name"]] = _name_count.get(it["name"], 0) + 1
    for it in disks:
        if _name_count.get(it["name"], 0) > 1:
            it["name"] = "%s·%s" % (it["name"], str(it["dev"]).replace("/dev/", ""))
    return jsonify({
        "cpu_temp": round(cpu_T, 1) if isinstance(cpu_T, (int, float)) else None,
        "mb_temp": round(mb_T, 1) if isinstance(mb_T, (int, float)) else None,
        "disks": disks,
        # 温度墙测点全量 + 阵列卡芯片温度：前端温度页 5s 实时刷新用（不再等 30s 快照）
        "sensors": _snap.get("temps") or [],
        "raid_temp": _snap.get("raid_temp"),
    })


@app.route("/api/fan/disk_temp")
def api_fan_disk_temp_get():
    """读取硬盘温度控风扇配置 + 实时监控盘温度/休眠 + 计算所得目标PWM"""
    cfg = _load_fan_disk_temp()
    devs = cfg.get("disks", [])
    states = get_disk_temps_cached(devs) if devs else {}
    disks_out = [{
        "dev": dev,
        "temp": states.get(dev, {}).get("temp"),
        "asleep": states.get(dev, {}).get("asleep"),
        "no_sleep": states.get(dev, {}).get("no_sleep", False),
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
    if "idle_minutes" in data:
        try:
            iv = float(data["idle_minutes"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "idle_minutes 需为数字"}), 400
        if iv < 1 or iv > 120:
            return jsonify({"ok": False, "error": "idle_minutes 需在 1~120 分钟"}), 400
        cfg["idle_minutes"] = iv
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


def _validate_fan_rule(r):
    """校验单条逐风扇规则，返回 (clean_rule|None, error|None)。value=None 表示删除该风扇规则。"""
    if r is None:
        return (None, None)
    if not isinstance(r, dict):
        return (None, "规则需为对象")
    src = r.get("source", "disk")
    if src not in _FAN_RULE_SOURCES and not (src.startswith("combo_max:") or src.startswith("combo_avg:")):
        return (None, "source 需为 disk / cpu / mb 或 combo_max:/combo_avg: 组合（如 combo_max:cpu,mb）")
    clean = {"enabled": bool(r.get("enabled", True)), "source": src}
    # active_mode：3按钮显式控速方案（'linear' 或 'curve'）。
    # 未提供时根据「是否有曲线」推断：有曲线→'curve'，否则→'linear'（兼容老配置）。
    if "active_mode" in r and r["active_mode"] in ("linear", "curve"):
        clean["active_mode"] = r["active_mode"]
    else:
        clean["active_mode"] = "curve" if r.get("curve") else "linear"
    for k, lo, hi, dv in (("start_temp", 0, 110, 40), ("full_temp", 0, 120, 60),
                          ("min_pwm", 0, 100, 30), ("max_pwm", 0, 100, 100),
                          ("recover_temp", 0, 120, 35)):
        v = r.get(k, dv)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return (None, k + " 需为数字")
        if v < lo or v > hi:
            return (None, "%s 需在 %d~%d" % (k, lo, hi))
        clean[k] = v
    if clean["full_temp"] <= clean["start_temp"]:
        return (None, "全速温度必须大于开转温度")
    if clean["recover_temp"] >= clean["start_temp"]:
        return (None, "恢复自动温度必须小于开转温度")
    # 可选：低于开转温度即强制停转（默认关）
    if "stop_below_start" in r:
        clean["stop_below_start"] = bool(r["stop_below_start"])
    if "curve" in r and r["curve"]:
        curve = r["curve"]
        if not isinstance(curve, list):
            return (None, "curve 需为数组")
        norm = []
        for p in curve:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                return (None, "curve 每项需为 [温度, 占空比]")
            try:
                t = float(p[0]); pw = float(p[1])
            except (TypeError, ValueError):
                return (None, "curve 温度/占空比需为数字")
            if pw < 0 or pw > 100:
                return (None, "curve 占空比需在 0~100")
            norm.append([t, pw])
        norm.sort(key=lambda x: x[0])
        clean["curve"] = norm
    return (clean, None)


@app.route("/api/fan/rules")
def api_fan_rules_get():
    """读取逐风扇温控规则（含旧全局面板派生的默认）+ 每台风扇的温度源、实时温度、计算目标。
    is_custom：是否已存在用户逐风扇覆盖文件。"""
    rules = _effective_fan_rules()
    dt = _load_fan_disk_temp()
    _dt_eff = dt
    if not dt.get("disks"):
        _all_devs = _list_all_disk_devs()
        if _all_devs:
            _dt_eff = dict(dt, disks=_all_devs)
    _need_disk_state = any(
        (r.get("source", "disk") == "disk") or "disk" in (r.get("source") or "") for r in rules.values())
    disk_all_idle, disk_T, disk_has = _disk_source_state(_dt_eff) if _need_disk_state else (False, None, bool(_dt_eff.get("disks")))
    cpu_T = _fan_read_sys_temp("cpu")
    mb_T = _fan_read_sys_temp("mb")
    fans_out = []
    for (hwmon, idx) in _enumerate_fans():
        rk = "%s::%d" % (hwmon, idx)
        rule = rules.get(rk)
        lbl = _load_fan_labels().get(rk, {})
        src = (rule or {}).get("source")
        if src == "cpu":
            T = cpu_T
        elif src == "mb":
            T = mb_T
        else:
            T = disk_T if rule else None
        computed = None
        if rule:
            raw = 0 if (src == "disk" and disk_all_idle) else _fan_rule_pwm(T, rule)
            if raw is not None:
                computed = round(raw / 255 * 100)
        fans_out.append({
            "hwmon": hwmon, "idx": idx, "key": rk,
            "name": lbl.get("name") or ("风扇%d" % idx),
            "hidden": bool(lbl.get("hidden")),
            "rule": rule,
            "source_temp": round(T, 1) if isinstance(T, (int, float)) else None,
            "computed_pwm": computed,
        })
    fans_out = _dedup_fan_names(fans_out)
    return jsonify({
        "fans": fans_out,
        "is_custom": _load_fan_rules_raw() is not None,
        "disk": {"disks": dt.get("disks", []), "has_disks": disk_has,
                 "all_idle": disk_all_idle, "temp": round(disk_T, 1) if isinstance(disk_T, (int, float)) else None},
        "cpu_temp": round(cpu_T, 1) if isinstance(cpu_T, (int, float)) else None,
        "mb_temp": round(mb_T, 1) if isinstance(mb_T, (int, float)) else None,
        "sources": list(_FAN_RULE_SOURCES) + ["combo_max:cpu,mb", "combo_avg:cpu,mb"],
    })


@app.route("/api/fan/rules", methods=["POST"])
@require_admin()
def api_fan_rules_set():
    """保存逐风扇温控规则覆盖。两种 body：
      整体覆盖：{"rules": {"<hwmon>::<idx>": {...}|null, ...}}
      单条设置：{"key": "<hwmon>::<idx>", "rule": {...}|null}
    rule=null 表示清除该风扇的自定义（回到全局默认/手动）。"""
    data = request.get_json(force=True, silent=True) or {}
    saved = _load_fan_rules_raw() or {"rules": {}}
    cur = dict(saved.get("rules") or {})
    # 注意：保存温度联动规则【不再】清掉该风扇的手动覆盖（移除旧 _clear_manual_for_rule）。
    # 手动与曲线是两套独立状态：手动只是临时钉转速，曲线(fan_rules)始终保留；
    # 用户点「回到我的曲线」即恢复（见 api_fan_set mode=auto）。两者不再互斥。

    if "key" in data:
        k = data.get("key")
        if not isinstance(k, str) or "::" not in k:
            return jsonify({"ok": False, "error": "key 需为 '<hwmon>::<idx>'"}), 400
        clean, err = _validate_fan_rule(data.get("rule"))
        if err:
            return jsonify({"ok": False, "error": err}), 400
        if clean is None:
            # 显式写入 null 占位：saved 里这条 key 不存在时 _derive_rules_from_legacy()
            # 仍会派生默认规则给该风扇，必须靠占位让 _effective_fan_rules() 的
            # `if r is None: rules.pop(k, None)` 把 legacy 默认也摘掉，关闭才真正生效。
            cur[k] = None
        else:
            cur[k] = clean
    elif "rules" in data:
        incoming = data.get("rules")
        if not isinstance(incoming, dict):
            return jsonify({"ok": False, "error": "rules 需为对象"}), 400
        for k, r in incoming.items():
            if not isinstance(k, str) or "::" not in k:
                return jsonify({"ok": False, "error": "规则 key 需为 '<hwmon>::<idx>'"}), 400
            clean, err = _validate_fan_rule(r)
            if err:
                return jsonify({"ok": False, "error": "%s: %s" % (k, err)}), 400
            if clean is None:
                # 同单条入口：必须写 null 占位，否则 legacy 派生默认会重新覆盖。
                cur[k] = None
            else:
                cur[k] = clean
    else:
        return jsonify({"ok": False, "error": "缺少 key 或 rules"}), 400
    if _save_fan_rules({"rules": cur}):
        return jsonify({"ok": True, "rules": cur})
    return jsonify({"ok": False, "error": "写配置失败"}), 500




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
    # 操作完成后立即刷新缓存，让前端拿到最新状态
    return jsonify({"ok": True, "status": _fcs_status_cached(clear=True)})


@app.route("/api/fan/labels", methods=["GET"])
def api_fan_labels_get():
    """返回用户标注的风扇名称/电压：key="hwmon::idx" -> {"name","voltage"}"""
    return jsonify(_load_fan_labels())


@app.route("/api/fan/labels", methods=["POST"])
@require_admin()
def api_fan_labels_post():
    """保存风扇标注（合并模式：仅覆盖传入的通道，保留其余，防止单条保存清空全部）。"""
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "bad json"}), 400
    incoming = {}
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
        entry = {}
        if name:
            entry["name"] = name
        if volt != "未知":
            entry["voltage"] = volt
        if v.get("hidden"):
            entry["hidden"] = True   # 隐藏无风扇的幽灵通道（可恢复）
        if v.get("no_tach"):
            # 用户标注「这把风扇没有转速反馈线」（2 针风扇 / 分线器副扇 / 转速线未接）：
            # 风扇本身在转，只是主板永远读不到 rpm。标了之后转速栏不再显示刺眼的 0，
            # 也不会被误判成「停转/空通道」。
            entry["no_tach"] = True
        # 空标注（name 空且无 hidden/no_tach）→ 视为取消该通道标注，删除键
        if not entry:
            incoming[k] = None
        else:
            incoming[k] = entry
    # 合并现有标注，而非整体覆盖：防御前端只传单条导致全量清空
    existing = _load_fan_labels()
    for k, v in incoming.items():
        if v is None:
            existing.pop(k, None)
        else:
            existing[k] = v
    _replicate_aliases(existing)   # 别名同步：同名标注通道改名/隐藏联动（huhaibo820 #1）
    if _save_fan_labels(existing):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "save failed"}), 500


@app.route("/api/version")
def api_version():
    """检测新版本：返回 {current, latest, update_available, url, error}。force=1 强制刷新缓存。"""
    if request.args.get("force") == "1":
        _VERSION_CHECK["checked_at"] = 0  # 使缓存失效，触发重查
    return jsonify(_check_latest_version())



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
    d.setdefault("level_channels", {
        "danger": ["system", "telegram", "bark", "email"],
        "warn": ["system", "telegram"],
        "info": ["system"],
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
    # 阵列卡告警（无硬件 VD 时 virtual_drives/drives 为空，自然不产生告警；本机 JBOD 直通降级安全）
    try:
        raid = get_raid_card()
    except Exception:
        raid = {}
    if raid and raid.get("mode") in ("mega", "hba"):
        cv = (raid.get("cachevault") or "").lower()
        if cv and cv not in ("optimal", "ok", "", "-", "none") and "optimal" not in cv:
            alerts.append({"level": "warn", "title": "CacheVault 状态异常", "detail": "阵列卡掉电保护缓存状态：%s（断电时缓存数据可能丢失）" % raid.get("cachevault")})
        for v in (raid.get("virtual_drives") or []):
            st = (v.get("state") or "").lower()
            if st and st not in ("optl", "optimal", "ok", ""):
                alerts.append({"level": "danger", "title": "逻辑盘状态异常", "detail": "%s 状态：%s" % (v.get("name") or v.get("dgvd"), v.get("state"))})
        # CopyBack 自动换盘监控：故障盘时提示换盘进度 / 待处理
        for d in (raid.get("drives") or []):
            if d.get("copyback_active"):
                alerts.append({"level": "danger", "title": "正在执行 CopyBack 换盘", "detail": "盘 %s 已故障并正在自动复制到热备盘（CopyBack 换盘中）" % d.get("slot")})
            elif d.get("failed"):
                hs = [h.get("slot") for h in (raid.get("hotspares") or [])]
                if hs:
                    tip = "开启自动 CopyBack 后将自动换盘" if raid.get("auto_copyback") == "enabled" else "需手动触发 CopyBack 换盘"
                    alerts.append({"level": "warn", "title": "硬盘故障待换盘", "detail": "盘 %s 已故障，已配置热备盘 %s，%s" % (d.get("slot"), "、".join(hs), tip)})
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

def _dispatch_notifications(text, cfg, level="danger"):
    """按「严重级别」把告警文本推送到该级别启用的渠道，返回 {channel: ok|error}。
    level_channels 决定每个级别走哪些渠道（danger/warn/info）；system 本地日志始终记录。"""
    res = {}
    ch = cfg.get("channels", {}) or {}
    lc = cfg.get("level_channels", {}) or {}
    allowed = lc.get(level, ["system"])
    if "telegram" in allowed and ch.get("telegram", {}).get("enabled"):
        t = ch["telegram"]
        res["telegram"] = _send_telegram(t.get("bot_token", ""), t.get("chat_id", ""), text)
    if "bark" in allowed and ch.get("bark", {}).get("enabled"):
        res["bark"] = _send_bark(ch["bark"].get("url", ""), text)
    if "email" in allowed and ch.get("email", {}).get("enabled"):
        res["email"] = _send_email(ch["email"], text)
    # system 本地日志始终记录（最低保障，不受级别过滤）
    _notify_log("通知[" + level + "]：" + text)
    res["system"] = True
    return res

def _read_app_log(max_lines=60):
    """读取应用运行日志尾部（供健康报告排查 bug 用）。找不到返回 None。"""
    candidates = [
        os.environ.get("TRIM_PKGVAR", "") + "/app.log",
        "/vol1/@appdata/com.dashboard.nasdash/app.log",
        "/var/apps/com.dashboard.nasdash/var/app.log",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()[-max_lines:]
                return "".join(lines)
            except Exception:
                pass
    return None


def _parse_log_entries(log_tail):
    """把运行日志解析成 [(时间戳, 级别, 内容)]，级别按内容推断，便于报告分级展示。

    ERROR：Traceback/Error/Exception/failed/失败/错误；WARN：warn/warning/警告；其余 INFO。
    无时间戳的行原样保留（时间戳为空串）。
    """
    entries = []
    for line in (log_tail or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[ ,]*(.*)$", s)
        ts = m.group(1) if m else ""
        text = (m.group(2) if m else s)
        low = text.lower()
        if any(k in low for k in ("traceback", "error", "exception", "failed", "失败", "错误")):
            lvl = "ERROR"
        elif any(k in low for k in ("warn", "warning", "警告")):
            lvl = "WARN"
        else:
            lvl = "INFO"
        entries.append((ts, lvl, text))
    return entries


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
        "log_tail": _read_app_log(),
    }

def build_diagnostics():
    """生成可复制粘贴的设备诊断包（供社区排障，隐私安全、不联网）。
    重点暴露『原生态』：每风扇通道的 hwmon::idx、pwm_enable 原始值、是否命中温控规则，
    以及全部风扇配置，便于远程定位『同风扇重复』『模式标签错显』『设温控后手动失效』等问题。"""
    try:
        fans = get_fan_status()
    except Exception:
        fans = []
    fan_rows = []
    for f in fans:
        fan_rows.append({
            "key": "%s::%d" % (f.get("hwmon"), f.get("idx")),
            "name": f.get("label") or f.get("name"),
            "pwm_enable": f.get("pwm_enable"),     # 原始寄存器：0/1/2
            "pwm_mode": f.get("pwm_mode"),         # 'pwm'(4pin脉宽) / 'dc'(3pin电压) / None
            "pwm_pct": f.get("pwm"),
            "rpm": f.get("rpm"),
            "mode": f.get("mode"),
            "rule_hit": bool(f.get("rule")),
            "rule_source": f.get("rule_source"),
            "computed_pwm": f.get("computed_pwm"),
            "target_pct": f.get("target_pct"),
            "no_tach": f.get("no_tach"),
            "hidden": f.get("hidden"),
        })
    # 疑似同物理风扇（相同非空转速出现在多个通道）→ 提示「一张卡变两张」问题（huhaibo820 #1）
    _rpm_groups = {}
    for f in fan_rows:
        r = f.get("rpm") or 0
        if r > 0:
            _rpm_groups.setdefault(r, []).append(f.get("key"))
    diag_dups = [v for v in _rpm_groups.values() if len(v) > 1]
    chips = []
    try:
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            chips.append(read_file(d + "/name").strip())
    except Exception:
        pass
    diag = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nasdash_version": APP_VERSION,
        "fnos_version": _fnos_version(),
        "kernel": getattr(os.uname(), "release", ""),
        "sensor_chips": chips,
        "fans": fan_rows,
        "fan_rules": _load_fan_rules_raw(),
        "fan_mode": _load_fan_modes(),
        "disk_temp": _load_fan_disk_temp(),
        "sys_temp": _load_fan_sys_temp(),
        "fan_labels": _load_fan_labels(),
        "alias_groups": _fan_alias_map(),
        "suspected_duplicate_rpm": diag_dups,
        "debug_log_tail": _debug_log_tail(60),
    }
    return diag

def render_diagnostics_text(diag):
    """把诊断包渲染成可复制的纯文本。"""
    L = []
    L.append("===== nasdash 设备诊断包 =====")
    L.append("生成时间: %s" % diag.get("generated_at"))
    L.append("nasdash 版本: %s" % diag.get("nasdash_version"))
    L.append("fnOS 版本: %s" % diag.get("fnos_version"))
    L.append("内核: %s" % diag.get("kernel"))
    L.append("传感器芯片: %s" % ", ".join(diag.get("sensor_chips") or []) or "无")
    L.append("")
    L.append("--- 风扇通道（key = hwmon::idx；pwm_enable: 0=关闭 1=软件控 2=交还自动；"
             "signal: PWM=4pin脉宽 DC=3pin电压）---")
    for f in diag.get("fans") or []:
        rule = ("命中[%s]" % f.get("rule_source")) if f.get("rule_hit") else "无规则"
        _sig = (f.get("pwm_mode") or "n/a").upper()
        _rpm_s = "无反馈线" if f.get("no_tach") else f.get("rpm")
        L.append("  %s | %s | pwm_enable=%s | signal=%s | pwm=%s%% | rpm=%s | mode=%s | %s | 目标=%s | 隐藏=%s"
                 % (f.get("key"), f.get("name"), f.get("pwm_enable"), _sig,
                    f.get("pwm_pct"), _rpm_s, f.get("mode"), rule,
                    f.get("target_pct"), f.get("hidden")))
    _dc = [f.get("key") for f in (diag.get("fans") or []) if f.get("pwm_mode") == "dc"]
    if _dc:
        L.append("ℹ 以下接口为 DC(电压)调速，占空比按电压比例输出、低档更易停转: %s" % ", ".join(_dc))
    # 无信号线风扇插在 PWM 口 → 收不到调速指令、恒满速。这是「怎么调都不降速」最常见的物理原因。
    _nt_pwm = [f.get("key") for f in (diag.get("fans") or [])
               if f.get("no_tach") and f.get("pwm_mode") == "pwm"]
    if _nt_pwm:
        L.append("⚠ 以下接口标注为『无转速反馈线(2/3针)』但当前是 PWM 调速：风扇收不到 PWM 信号线指令，"
                 "占空比很可能不起作用（恒满速）。改 DC 模式 / 换 4 针风扇可解: %s" % ", ".join(_nt_pwm))
    if diag.get("suspected_duplicate_rpm"):
        L.append("⚠ 疑似同物理风扇（相同转速多通道，疑似「一张卡变两张」）: %s"
                 % json.dumps(diag.get("suspected_duplicate_rpm"), ensure_ascii=False))
    if diag.get("alias_groups"):
        L.append("已合并同名通道(别名组): %s" % json.dumps(diag.get("alias_groups"), ensure_ascii=False))
    L.append("")
    L.append("--- 风扇配置 ---")
    L.append("fan_rules: %s" % json.dumps(diag.get("fan_rules"), ensure_ascii=False))
    L.append("fan_mode: %s" % json.dumps(diag.get("fan_mode"), ensure_ascii=False))
    L.append("disk_temp: %s" % json.dumps(diag.get("disk_temp"), ensure_ascii=False))
    L.append("sys_temp: %s" % json.dumps(diag.get("sys_temp"), ensure_ascii=False))
    L.append("fan_labels: %s" % json.dumps(diag.get("fan_labels"), ensure_ascii=False))
    L.append("")
    L.append("--- debug.log 尾部（脱敏）---")
    for ln in diag.get("debug_log_tail") or []:
        L.append("  " + ln)
    L.append("=========================")
    return "\n".join(L)

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
    .logstat{padding:9px 12px;border:1px solid #d4dce6;border-top:none;background:#f4f8fb;
         font-size:12.5px;margin-bottom:4px}
    .logstat b{font-size:13px}
    .log-wrap{border:1px solid #d4dce6;border-top:none;margin-bottom:4px;max-height:380px;overflow:auto}
    .log-line{font-family:Consolas,Menlo,monospace;font-size:11.5px;padding:4px 12px;
         border-bottom:1px solid #eef1f5;white-space:pre-wrap;word-break:break-all;line-height:1.5}
    .log-line:last-child{border-bottom:none}
    .log-line .lt{color:#8a97a5;margin-right:8px}
    .log-line.lv-err{background:#fdf0ef;color:#a93226}
    .log-line.lv-warn{background:#fdf6ec;color:#9a6700}
    .log-line.lv-info{color:#3d5167}
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

    # 运行日志（诊断）：解析分级，错误/警告优先展示，与上方告警呼应，便于看 bug
    log_entries = _parse_log_entries(rep.get("log_tail"))
    n_err = sum(1 for _, lvl, _ in log_entries if lvl == "ERROR")
    n_warn = sum(1 for _, lvl, _ in log_entries if lvl == "WARN")
    if log_entries:
        log_rows_html = "".join(
            f"<div class='log-line lv-{lvl.lower()}'><span class='lt'>{esc(ts)}</span>{esc(text)}</div>"
            for ts, lvl, text in log_entries)
        if n_err or n_warn:
            stat = (f"运行日志共 {len(log_entries)} 行：<b class='danger'>错误 {n_err}</b> · "
                    f"<b class='warn'>警告 {n_warn}</b> · 常规 {len(log_entries) - n_err - n_warn}"
                    f"（下方按级别着色，错误/警告优先置顶展示）")
            # 错误/警告行优先，常规行放最后
            log_entries_sorted = sorted(log_entries, key=lambda e: 0 if e[1] == "ERROR" else (1 if e[1] == "WARN" else 2))
            log_rows_html = "".join(
                f"<div class='log-line lv-{lvl.lower()}'><span class='lt'>{esc(ts)}</span>{esc(text)}</div>"
                for ts, lvl, text in log_entries_sorted)
        else:
            stat = f"运行日志共 {len(log_entries)} 行，最近日志无错误 ✓（常规信息）"
        log_html = f"<div class='logstat'>{stat}</div><div class='log-wrap'>{log_rows_html}</div>"
    else:
        log_html = "<div class='empty'>（暂无运行日志）</div>"

    sys_rows = [
        ["CPU 型号", fmt(sys_.get("cpu_model"))],
        ["CPU 核心/线程", f"{fmt(sys_.get('cpu_cores'))} / {fmt(sys_.get('cpu_threads'))}"],
        ["CPU 频率", fmt(sys_.get("cpu_freq"), " MHz")],
        ["负载 (1/5/15)", " / ".join(fmt(x) for x in (sys_.get("load") or []))],
        ["内存", f"{fmt(mem.get('used'))} / {fmt(mem.get('total'))}（{fmt(mem.get('percent'))}%）"],
        ["交换分区", f"{fmt(swap.get('used'))} / {fmt(swap.get('total'))}"],
        ["显卡", fmt("、".join((g.get("name") or g.get("type") or "") for g in gpus) if gpus else "—")],
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
        + cat("运行日志（诊断）")
        + log_html
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
    if "level_channels" in data and isinstance(data["level_channels"], dict):
        lc = cfg.get("level_channels", {})
        for lvl in ("danger", "warn", "info"):
            if lvl in data["level_channels"]:
                val = data["level_channels"][lvl]
                if isinstance(val, list):
                    lc[lvl] = [c for c in val if c in ("system", "telegram", "bark", "email")]
        cfg["level_channels"] = lc
    if _save_alerts(cfg):
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": False, "error": "写配置失败"}), 500

@app.route("/api/alerts/test", methods=["POST"])
@require_admin()
def api_alerts_test():
    cfg = _load_alerts()
    text = "这是一条来自 nasdash 的测试通知（当前版本 %s）。若收到说明通知渠道配置正确。" % APP_VERSION
    res = _dispatch_notifications(text, cfg, "danger")
    return jsonify({"ok": True, "results": res})

@app.route("/api/report")
def api_report():
    fmt = request.args.get("format", "json")
    rep = build_health_report()
    if fmt == "html":
        return _render_report_html(rep)
    return jsonify(rep)

@app.route("/api/diagnostics")
def api_diagnostics():
    """设备诊断包：txt=可复制纯文本（贴论坛用），json=结构化。隐私安全、不联网。"""
    fmt = request.args.get("format", "txt")
    diag = build_diagnostics()
    if fmt == "json":
        return jsonify(diag)
    return Response(render_diagnostics_text(diag), mimetype="text/plain; charset=utf-8")

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
