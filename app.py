#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 硬件监控面板 (fnOS Hardware Dashboard)
单文件 Flask 应用：阵列卡状态 / 硬盘 SMART / 系统资源 / 存储卷
部署目录: /opt/fnos-dash/
"""
import subprocess, json, re, os, time, socket, platform, shutil, sys, urllib.request, urllib.error
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# 应用根目录
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 用户配置持久目录：飞牛运行时通过环境变量 TRIM_PKGVAR 提供 @appdata 持久目录
# （与应用卸载无关，重装后保留；cmd/main 也用它存 app.pid/app.log）。
# 早期版本把配置写在 APP_DIR，导致每次重装被清空。现统一写入此持久目录，重装不丢配置。
def _config_dir():
    d = os.environ.get("TRIM_PKGVAR")
    if not d:
        d = "/usr/local/apps/@appdata/com.dashboard.nasdash"
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return APP_DIR

# 从旧版(配置存 APP_DIR)升级时，把已有配置迁移到持久目录，避免丢失
def _migrate_legacy_configs():
    cfg = _config_dir()
    if cfg == APP_DIR:
        return
    for name in ("board_override.txt", "fan_labels.json", "fan_disk_temp.json", "fan_sys_temp.json"):
        src = os.path.join(APP_DIR, name)
        dst = os.path.join(cfg, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass

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

def run(cmd, timeout=30):
    """执行 shell 命令，返回 stdout 字符串；失败时记录 stderr 便于排查（不再静默吞掉）"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and r.stderr.strip():
            log(f"cmd failed (rc={r.returncode}): {cmd}\n{r.stderr.strip()}")
        return r.stdout
    except Exception as e:
        log(f"cmd error: {cmd}\n{e}")
        return ""

def sudo(cmd, timeout=30):
    # 已是 root 直接跑，省去 sudo（fnOS 应用常以 root 运行）
    if os.geteuid() == 0:
        return run(cmd, timeout)
    out = run("sudo -n " + cmd, timeout)
    if out.strip():
        return out
    return run(cmd, timeout)  # sudo -n 失败再裸跑兜底

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
    # 检测系统风扇服务（FanControlServer）是否在跑（恢复自动时优先交还）。
    # 注意：pgrep -f 会把「模式串出现在自身命令行」也匹配上 → 永远返回 True（假阳性），
    # 导致没装 FCS 的机器也误判为已安装、把风扇错误交还给主板原生自动（SYS_FAN2 等口曲线偏激进会狂转）。
    # 用 [f]an... 括号技巧避免自匹配。
    return bool(run("pgrep -f '[f]ancontrolserver' 2>/dev/null", 2).strip())

def _fan_read_cpu_temp():
    try:
        out = run("sensors -j 2>/dev/null", 5)
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

def fan_smooth_loop():
    # daemon 线程：每 ~0.6s 把风扇当前 pwm 朝目标平滑过渡（常驻线程 tick + 缓变）
    while True:
        try:
            with FAN_LOCK:
                overrides = dict(FAN_TARGETS)   # 每风扇手动/自动覆盖（仅用户经 UI 调过的风扇）
            all_fans = _enumerate_fans()          # 本机真实风扇全集（it87/nct）
            st = _load_fan_sys_temp()
            dt = _load_fan_disk_temp()
            sys_claimed, disk_claimed = _select_temp_fans(all_fans, st, dt)
            controlled = sys_claimed | disk_claimed
            # 主板/CPU 温控（sys_temp）：优先级最高，先接管
            if st.get("enabled"):
                T = _fan_read_sys_temp(st.get("source", "cpu"))
                action, target = _fan_sys_temp_decision(T, st)
                for (hwmon, idx) in sys_claimed:
                    if action == "control" and target is not None:
                        _fan_smooth_step(hwmon, idx, target)
                    elif action == "release":
                        _fan_release_auto(hwmon, idx)
                    # "hold" → 已交还自动，不再写入（避免与主板/内核抢控）
            # 硬盘温度控制（disk_temp）：接管未被 sys_temp 占用的受控风扇
            if dt.get("enabled") and dt.get("disks"):
                states = get_disk_temps(dt["disks"])
                action, target = _fan_disk_temp_decision(states, dt)
                for (hwmon, idx) in disk_claimed:
                    if action == "control" and target is not None:
                        _fan_smooth_step(hwmon, idx, target)
                    elif action == "release":
                        _fan_release_auto(hwmon, idx)
                    # "hold" → 已交还自动，不再写入
            # 剩余风扇：仅处理用户在 UI 中手动/自动设过的（overrides）；未触碰的风扇保持原样（交还 BIOS/主板）
            for (hwmon, idx), cfg in overrides.items():
                if (hwmon, idx) in controlled:
                    continue  # 已被温控接管
                if cfg.get("mode") == "auto":
                    ct = _fan_cpu_temp_cached()
                    _fan_smooth_step(hwmon, idx, _fan_auto_pwm(ct))
                else:
                    tgt = cfg.get("target")
                    if tgt is None:
                        continue
                    _fan_smooth_step(hwmon, idx, tgt)
        except Exception:
            pass
        time.sleep(0.6)

_fan_thread = _threading.Thread(target=fan_smooth_loop, daemon=True, name="fan-smooth")
_fan_thread.start()

# ===================== 风扇标注（用户可编辑名称/电压，按安装实例持久化）=====================
# 标注与硬件无关：只存 (hwmon, idx) -> {name, voltage}，不写死任何机型，对所有用户（含 IT87）安全。
FAN_LABELS_FILE = os.path.join(_config_dir(), "fan_labels.json")
_FAN_VOLT_ALLOWED = ("12V", "5V", "未知", "")

def _load_fan_labels():
    try:
        with open(FAN_LABELS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}

def _save_fan_labels(d):
    try:
        with open(FAN_LABELS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

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
    try:
        with open(FAN_DISK_TEMP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in defaults:
                if k in d:
                    defaults[k] = d[k]
    except Exception:
        pass
    return defaults

def _save_fan_disk_temp(cfg):
    try:
        with open(FAN_DISK_TEMP_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

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
    try:
        with open(FAN_SYS_TEMP_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            for k in defaults:
                if k in d:
                    defaults[k] = d[k]
    except Exception:
        pass
    return defaults

def _save_fan_sys_temp(cfg):
    try:
        with open(FAN_SYS_TEMP_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False

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
        out = run("sensors -j 2>/dev/null", 5)
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

def _fan_sys_temp_pwm(T, cfg):
    """按单值温度 T 算目标 raw(0~255)。T<start→0；start≤T<full→min~max 线性；T≥full→max。"""
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
                out = sudo(f"{SMARTCTL} -A {dev} 2>/dev/null", 8)
                asleep = False
            else:
                out = sudo(f"{SMARTCTL} -n standby -A {dev} 2>/dev/null", 8)
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
    - 取最热盘温度 T：T<start → 0；start≤T<full → min~max 线性；T≥full → max
    """
    start = float(cfg.get("start_temp", 40))
    full = float(cfg.get("full_temp", 60))
    minp = float(cfg.get("min_pwm", 30))
    maxp = float(cfg.get("max_pwm", 70))
    sleep_stop = bool(cfg.get("sleep_stop", True))
    valid = [s for s in (states or {}).values() if isinstance(s, dict)]
    if not valid:
        return None
    if sleep_stop and all(s.get("asleep") for s in valid):
        return 0
    temps = [s["temp"] for s in valid if isinstance(s.get("temp"), (int, float))]
    if not temps:
        return round(minp / 100 * 255)
    T = max(temps)
    if T < start:
        return 0
    if T >= full:
        return round(maxp / 100 * 255)
    r = (T - start) / (full - start)
    raw = minp + r * (maxp - minp)
    return round(raw / 100 * 255)

# 硬盘温控滞回状态：None=未初始化, True=nasdash 接管控速, False=已交还主板自动
_dt_engaged = {"v": None}

def _fan_release_auto(hwmon, idx):
    """把风扇交还主板/内核自动控速（pwm_enable=2）。FCS 若存在会重新接管。"""
    try:
        with open(f"{hwmon}/pwm{idx}_enable", "w") as f:
            f.write("2")
        return True
    except Exception:
        return False

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
    out = run("lspci -nn 2>/dev/null", 10)
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
            out = sudo(f"smartctl -i /dev/{d}", 8)
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


def get_raid_card():
    data = {"ok": False, "mode": "none", "model": "未检测到",
            "drives": [], "raw": "", "note": "", "controllers": []}
    out = sudo(f"{STORCLI} /c0 show", 30)
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
            temp = _parse_roc_temp(sudo(f"{STORCLI} /c0 show all", 15))
        if temp is None:
            # LSI-9300 等 HBA 卡 /c0 show 不含温度，必须单独跑 /c0 show temperature
            temp = _parse_roc_temp(sudo(f"{STORCLI} /c0 show temperature", 10))
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
                    sn_out = sudo(f"{STORCLI} /c0 /e{e} /s{s} show all", 15)
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
            data["controller_temp"] = _parse_roc_temp(sudo(f"{STORCLI} /c0 show temperature", 10)) \
                or _parse_roc_temp(sudo(f"{STORCLI} /c0 show", 30))
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

def get_disks():
    """采集所有块设备 + SMART（SD/SAS 用 ls /dev/sd*，NVMe 用 ls /dev/nvme*；再用正则过滤掉分区/控制器，
    支持多位盘名 sdaa/sdab 与多控制器 nvme10n1 等；smartctl 拿详情，不依赖 lsblk 字段对齐）"""
    disks = []
    out = run("ls /dev/sd* 2>/dev/null", 5)
    devnames = sorted(set(l.strip().split('/')[-1] for l in out.split()
                          if l.strip() and re.match(r"^sd[a-z]+$", l.strip().split('/')[-1])))
    # NVMe 命名空间（如 /dev/nvme0n1；控制器 /dev/nvme0 不匹配 n\d+，不会误纳入）
    nvme_out = run("ls /dev/nvme* 2>/dev/null", 5)
    for l in nvme_out.split():
        n = l.strip().split('/')[-1]
        if re.match(r"^nvme\d+n\d+$", n):
            devnames.append(n)
    devnames = sorted(set(devnames))
    # lsblk 补充容量/rota/tran（-n 不打印表头，但仍防御性跳过首行若为表头）
    lsblk = run("lsblk -dn -b -o NAME,SIZE,ROTA,TRAN 2>/dev/null", 5)
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
        smart_out = sudo(f"{SMARTCTL} -n standby -a {dev}", 20)
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
    out = sudo("lspci 2>/dev/null", 5)
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


def get_board():
    """主板信息：优先 /sys/class/dmi/id（免 root），空则 dmidecode；
    DMI 全空（准系统/工控白牌板常见）时尝试读取手动标注，最后回退芯片组识别。"""
    def _read_dmi_sysfs(name):
        v = run(f"cat /sys/class/dmi/id/{name} 2>/dev/null", 2).strip()
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
        out = sudo(f"{DMIDECODE} -t 2 2>/dev/null", 8)
        if not out:
            out = sudo(f"{DMIDECODE} -t 1 2>/dev/null", 8)
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
    dd = run("command -v decode-dimms 2>/dev/null", 5).strip()
    if not dd:
        return []
    out = sudo(f"{dd} 2>/dev/null", 15)
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
    out = sudo(f"{DMIDECODE} -t 17 2>/dev/null", 12)
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
    lscpu = run("lscpu", 5)
    m = re.search(r"Model name:\s*(.+)", lscpu)
    d["cpu_model"] = m.group(1).strip() if m else "?"
    m = re.search(r"CPU\(s\):\s*(\d+)", lscpu)
    d["cpu_threads"] = int(m.group(1)) if m else 0
    m = re.search(r"Core\(s\) per socket:\s*(\d+)", lscpu)
    d["cpu_cores"] = int(m.group(1)) if m else 0
    m = re.search(r"CPU max MHz:\s*([\d.]+)", lscpu)
    d["cpu_freq"] = m.group(1) if m else "?"
    # 负载
    la = run("cat /proc/loadavg", 3).split()
    d["load"] = la[:3] if len(la) >= 3 else ["0","0","0"]
    # uptime
    up = run("cat /proc/uptime", 3).split()
    try:
        up_s = float(up[0])
        d["uptime"] = format_uptime(up_s)
    except:
        d["uptime"] = "?"
    # 内存
    meminfo = run("cat /proc/meminfo", 3)
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
    sens_j = run(f"{SENSORS} -j 2>/dev/null", 8)
    d["sensors"] = {"temps": [], "fans": [], "voltages": []}
    cpu_temp = None
    # 风扇控制信息：优先系统风扇服务配置，其次 sysfs（不依赖任何外部应用）
    fan_info = {}
    # 1) 系统风扇服务配置（可选）—— 提供风扇名称/模式，并借 pwm_path 推断可写路径
    fc_raw = run("cat /vol2/@appconf/FanControlServer/config.json 2>/dev/null", 3)
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
        _pe = run(f"cat {_hw}/pwm{_fi}_enable 2>/dev/null", 2).strip()
        _pv = run(f"cat {_hw}/pwm{_fi} 2>/dev/null", 2).strip()
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
    lspci = run("lspci 2>/dev/null", 5)
    gpus = []
    for line in lspci.splitlines():
        if re.search(r"VGA compatible controller|3D controller|Display controller", line, re.I):
            m = re.search(r"controller:\s*(.+)", line, re.I)
            gpus.append(m.group(1).strip() if m else line.strip())
    d["gpus"] = gpus
    # 网卡（只显示物理网卡和 bond，过滤 docker/虚拟网桥）
    link_out = run("ip -o link show 2>/dev/null", 5)
    addr_out = run("ip -o addr show 2>/dev/null", 5)
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
        speed = run(f"cat /sys/class/net/{name}/speed 2>/dev/null", 2).strip()
        if not speed.isdigit():
            speed = ""
        ip = ""
        for aline in addr_out.splitlines():
            if re.search(rf"\b{re.escape(name)}\b", aline):
                im = re.search(r"\binet\s+(\S+)", aline)
                if im and not ip:
                    ip = im.group(1).split("/")[0]
        nics.append({"name": name, "state": state, "mac": mac, "speed": speed, "ip": ip})
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
    mdstat = run("cat /proc/mdstat", 3)
    d["mdstat"] = mdstat
    d["topology"] = sudo("lsblk -o NAME,SIZE,TYPE,ROTA,MODEL 2>/dev/null", 5) or run("lsblk", 5)
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
    df = run("df -h --output=target,size,used,avail,pcent,fstype 2>/dev/null", 5)
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
    out = sudo(f"docker top {name}", 5)
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

def get_docker():
    """统计 Docker 容器数（运行中/总数），并自动探测每个容器真实监听端口"""
    try:
        out = sudo("docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'", 8)
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
            containers.append({"name": name, "status": status, "image": image, "ports": "-", "running": running, "mem": None, "runtime": _cn_status(status)})
        # 批量 inspect 取端口 / pid / 网络模式，自动探测端口
        try:
            ids = sudo("docker ps -a -q", 8).split()
            if ids:
                raw = sudo("docker inspect " + " ".join(ids), 15)
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
                out2 = sudo("docker ps -a --format '{{.Names}}|{{.Ports}}'", 8)
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
        # 运行中容器的内存占用（docker stats 仅对运行中容器有数据）
        try:
            stat = sudo("docker stats --no-stream --format '{{.Name}}|{{.MemUsage}}'", 8)
            for line in stat.splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                cname, mem = line.split("|", 1)
                cname = cname.strip()
                for c in containers:
                    if c["name"] == cname:
                        c["mem"] = mem.strip()
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
    return render_template_string(HTML, APP_VERSION=APP_VERSION)

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
    except Exception as e:
        result = {"error": str(e), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    return jsonify(result)

@app.route("/api/fan/set", methods=["POST"])
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
        if idx < 1 or idx > 9:
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
            return jsonify({"ok": True, "mode": "auto", "owner": "ext_service"})
        # 系统风扇服务未运行：nasdash 自带保守温控曲线接管
        with FAN_LOCK:
            FAN_TARGETS[key] = {"mode": "auto", "target": None}
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
    return jsonify({"ok": True, "mode": "manual", "pwm": pct, "raw": raw})


@app.route("/api/fan/status")
def api_fan_status():
    """轻量风扇状态：供前端高频轮询，实时显示转速/当前占空比/目标（常驻线程 2s tick）"""
    import glob as _glob
    fans = []
    labels = _load_fan_labels()
    _dt = _load_fan_disk_temp()
    _dt_active = bool(_dt.get("enabled")) and bool(_dt.get("disks"))
    _dt_cf = _dt.get("controlled_fans", "all")
    _st = _load_fan_sys_temp()
    _st_active = bool(_st.get("enabled"))
    _st_cf = _st.get("controlled_fans", "all")
    fc_raw = run("cat /vol2/@appconf/FanControlServer/config.json 2>/dev/null", 3)
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
        _pe = run(f"cat {hwmon}/pwm{idx}_enable 2>/dev/null", 2).strip()
        _pv = run(f"cat {hwmon}/pwm{idx} 2>/dev/null", 2).strip()
        _fv = run(f"cat {hwmon}/fan{idx}_input 2>/dev/null", 2).strip()
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
    return jsonify({"fans": fans})


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
def api_fan_disk_temp_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_fan_disk_temp()
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
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
        if rv < 0 or rv > 100:
            return jsonify({"ok": False, "error": "recover_temp 需在 0~100"}), 400
        cfg["recover_temp"] = rv
    if "sleep_stop" in data:
        cfg["sleep_stop"] = bool(data["sleep_stop"])
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
    if cfg.get("full_temp", 60) <= cfg.get("start_temp", 40):
        return jsonify({"ok": False, "error": "full_temp 必须大于 start_temp"}), 400
    if cfg.get("recover_temp", 35) >= cfg.get("start_temp", 40):
        return jsonify({"ok": False, "error": "recover_temp 必须小于 start_temp"}), 400
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
def api_fan_sys_temp_set():
    data = request.get_json(force=True, silent=True) or {}
    cfg = _load_fan_sys_temp()
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if "source" in data:
        if data["source"] not in ("cpu", "mb"):
            return jsonify({"ok": False, "error": "source 需为 cpu 或 mb"}), 400
        cfg["source"] = data["source"]
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
        if rv < 0 or rv > 120:
            return jsonify({"ok": False, "error": "recover_temp 需在 0~120"}), 400
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
    if cfg.get("full_temp", 70) <= cfg.get("start_temp", 45):
        return jsonify({"ok": False, "error": "full_temp 必须大于 start_temp"}), 400
    if cfg.get("recover_temp", 40) >= cfg.get("start_temp", 45):
        return jsonify({"ok": False, "error": "recover_temp 必须小于 start_temp"}), 400
    if _save_fan_sys_temp(cfg):
        return jsonify({"ok": True, "config": cfg})
    return jsonify({"ok": False, "error": "写配置失败"}), 500




@app.route("/api/fan/labels", methods=["GET"])
def api_fan_labels_get():
    """返回用户标注的风扇名称/电压：key="hwmon::idx" -> {"name","voltage"}"""
    return jsonify(_load_fan_labels())


@app.route("/api/fan/labels", methods=["POST"])
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


# ===================== 前端 =====================
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>飞牛 NAS 硬件监控</title>
<style>
  :root{
    --bg:#f0f2f5; --card:#ffffff; --border:#e5e7eb; --text:#1f2937; --muted:#6b7280;
    --blue:#2563eb; --green:#16a34a; --orange:#ea580c; --red:#dc2626; --yellow:#ca8a04;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
  .header{background:linear-gradient(135deg,#1e3a5f,#2563eb);color:#fff;padding:18px 28px;display:flex;align-items:center;justify-content:space-between;box-shadow:0 2px 8px rgba(0,0,0,.1)}
  .header h1{font-size:20px;font-weight:600}
  .header .meta{font-size:13px;opacity:.9;display:flex;gap:16px;align-items:center}
  .container{max-width:1600px;margin:0 auto;padding:20px;display:flex;gap:20px;align-items:flex-start}
  .sidebar{width:210px;flex:0 0 210px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px;box-shadow:0 1px 3px rgba(0,0,0,.06);position:sticky;top:20px}
  .sidebar-title{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;padding:6px 10px 10px}
  .tabs{display:flex;flex-direction:column;gap:4px}
  .tab{width:100%;padding:12px 14px;text-align:left;cursor:pointer;border-radius:8px;font-size:14px;font-weight:500;color:var(--muted);transition:all .2s;border:none;background:none;display:flex;align-items:center;gap:10px}
  .tab:hover{background:#f3f4f6}
  .tab.active{background:var(--blue);color:#fff}
  .content{flex:1;min-width:0}
  .panel{display:none}
  .panel.active{display:block}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
  .card{background:var(--card);border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.06);border:1px solid var(--border)}
  .card h3{font-size:13px;color:var(--muted);margin-bottom:10px;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
  .kv{display:flex;justify-content:space-between;padding:4px 0;font-size:14px;border-bottom:1px solid #f3f4f6;gap:12px}
  .kv:last-child{border-bottom:none}
  .kv .k{color:var(--muted);flex:0 0 auto;max-width:60%}
  .kv .v{font-weight:500;text-align:right;word-break:break-all;flex:1;min-width:0}
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}
  .b-ok{background:#dcfce7;color:var(--green)}
  .b-warn{background:#fef3c7;color:var(--yellow)}
  .b-bad{background:#fee2e2;color:var(--red)}
  .b-info{background:#dbeafe;color:var(--blue)}
  .disk-card{position:relative}
  .disk-card.bad{border-color:var(--red);background:#fef2f2}
  .disk-card.warn{border-color:var(--orange)}
  .disk-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .disk-head .name{font-size:16px;font-weight:600}
  .temp-bar{height:6px;border-radius:3px;background:#e5e7eb;margin:8px 0;overflow:hidden}
  .temp-fill{height:100%;border-radius:3px;transition:width .3s}
  .table{width:100%;border-collapse:collapse;font-size:13px}
  .table th,.table td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
  .table th{color:var(--muted);font-weight:500;background:#f9fafb}
  .table tr:hover{background:#f9fafb}
  .progress{height:20px;background:#e5e7eb;border-radius:10px;overflow:hidden;position:relative}
  .progress-fill{height:100%;display:flex;align-items:center;justify-content:flex-end;padding-right:8px;color:#fff;font-size:11px;font-weight:600}
  .btn{padding:6px 14px;border:1px solid rgba(255,255,255,.4);background:rgba(255,255,255,.15);color:#fff;border-radius:6px;cursor:pointer;font-size:13px}
  .btn:hover{background:rgba(255,255,255,.25)}
  .switch{display:inline-flex;align-items:center;gap:6px;font-size:13px;cursor:pointer}
  .switch input{display:none}
  .slider{width:34px;height:18px;background:rgba(255,255,255,.3);border-radius:10px;position:relative;transition:.2s}
  .slider:before{content:"";position:absolute;width:14px;height:14px;background:#fff;border-radius:50%;top:2px;left:2px;transition:.2s}
  .switch input:checked+.slider{background:#16a34a}
  .switch input:checked+.slider:before{transform:translateX(16px)}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .6s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .loading{text-align:center;padding:40px;color:var(--muted)}
  pre{background:#1e293b;color:#e2e8f0;padding:14px;border-radius:8px;font-size:12px;overflow-x:auto;max-height:400px;white-space:pre-wrap}
  .section-title{font-size:15px;font-weight:600;margin:18px 0 10px;color:var(--text)}
  .pill{display:inline-block;padding:1px 8px;border-radius:6px;font-size:11px;margin-left:6px}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .grid-auto{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
  .detect-title{font-size:22px;font-weight:700;margin-bottom:2px}
  .detect-sub{font-size:13px;color:var(--muted);margin-bottom:18px}
  .statusbar{display:flex;justify-content:space-between;align-items:center;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 18px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .status-left{display:flex;align-items:center;gap:8px}
  .status-dot{width:9px;height:9px;border-radius:50%;background:var(--green)}
  .status-right{display:flex;gap:26px;align-items:center}
  .status-item{display:flex;flex-direction:column;gap:2px}
  .status-item .k{font-size:11px;color:var(--muted)}
  .status-item .v{font-size:13px;font-weight:600}
  .detect-cards{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .disk-mini{display:flex;justify-content:space-between;padding:5px 0;font-size:13px;border-bottom:1px solid #f3f4f6;gap:10px}
  .disk-mini:last-child{border-bottom:none}
  .disk-group-title{font-size:12px;font-weight:600;color:#6b7280;margin:10px 0 4px;padding-left:2px}
  .disk-group-title:first-child{margin-top:0}
  .b-sas{background:#1e40af;color:#fff}
  .b-sata{background:#15803d;color:#fff}
  @media(max-width:768px){
    .header{flex-direction:column;align-items:flex-start;gap:10px;padding:14px 16px}
    .header h1{font-size:18px}
    .meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
    .container{flex-direction:column;padding:12px 14px 20px;gap:14px}
    .sidebar{width:100%;flex:auto;position:static}
    .content{min-width:0}
    .statusbar{flex-direction:column;align-items:stretch;gap:10px;padding:12px 14px;margin-bottom:14px}
    .status-right{flex-wrap:wrap;width:100%;gap:12px 18px}
    .status-item{min-width:42%}
    .detect-cards,.cards{grid-template-columns:1fr;gap:12px}
    .detect-cards>*,.cards>*{grid-column:auto!important}
    .grid-2,.grid-auto{grid-template-columns:1fr}
    .card{padding:14px}
    .kv{gap:8px}
    .kv .v{word-break:break-word;overflow-wrap:anywhere}
    .detect-title{font-size:18px}
    .detect-sub{margin-bottom:12px}
    .table{font-size:12px}
    .table th,.table td{padding:7px 8px}
  }
  .fan-ctrl-wrap{margin-top:10px;border-top:1px dashed var(--border);padding-top:10px}
  .fan-ctrl-list{display:flex;flex-direction:column;gap:14px}
  .fan-ctrl-head{display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px}
  .fan-ctrl-val{font-weight:600;color:var(--blue)}
  .fan-slider{width:100%;accent-color:var(--blue)}
  .fan-ctrl-actions{display:flex;align-items:center;gap:10px;margin-top:6px}
  .fan-ctrl-state{font-size:12px;color:var(--muted)}
  .btn-mini{padding:3px 10px;font-size:12px;border:1px solid var(--border);border-radius:6px;background:var(--card);cursor:pointer;color:var(--text)}
  .btn-mini:hover{background:var(--bg)}
  .btn-mini.b-ok{background:var(--green);color:#fff;border-color:var(--green)}
  .btn-mini.b-bad{background:var(--red);color:#fff;border-color:var(--red)}
  .edit-pencil{float:right;cursor:pointer;color:var(--blue);font-size:13px;font-weight:400}
  .edit-pencil:hover{opacity:.7}
  .inp{padding:6px 8px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text)}
  /* 风扇控制独立页面 */
  .fan-page{display:flex;flex-direction:column;gap:16px}
  .fan-presets{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:10px}
  .fan-presets .label{font-size:13px;color:var(--muted);margin-right:2px}
  .preset-btn{padding:8px 13px;font-size:13px;font-weight:600;border:1px solid var(--border);border-radius:8px;background:var(--bg);cursor:pointer;color:var(--text);transition:.15s}
  .preset-btn:hover{border-color:var(--blue);color:var(--blue)}
  .preset-btn.full{background:var(--red);color:#fff;border-color:var(--red)}
  .preset-btn.full:hover{opacity:.85;color:#fff}
  .preset-btn.active{background:var(--blue);color:#fff;border-color:var(--blue);box-shadow:0 0 0 2px rgba(57,125,255,.25)}
  .preset-btn.full.active{background:var(--red);border-color:var(--red);box-shadow:0 0 0 2px rgba(220,53,69,.25)}
  .fan-global-state{font-size:13px;color:var(--muted);margin-left:auto}
  .fan-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
  .fan-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
  .fan-card-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:6px}
  .fan-card-name{font-size:14px;font-weight:600}
  .fan-label-input{flex:1 1 140px;min-width:120px;padding:5px 8px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}
  .fan-label-input:focus{outline:none;border-color:var(--blue)}
  .fan-label-save{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}
  .fan-volt-select{padding:4px 6px;font-size:12px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}
  .fan-volt-select:focus{outline:none;border-color:var(--blue)}
  .fan-label-state{font-size:12px;color:var(--green)}
  .volt-badge{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:10px;background:var(--bg);color:var(--muted);border:1px solid var(--border);margin-left:4px}
  .fan-rpm{font-size:26px;font-weight:700;color:var(--blue);line-height:1.1}
  .fan-rpm small{font-size:12px;color:var(--muted);font-weight:400;margin-left:4px}
  .fan-speed-bar{height:8px;background:var(--bg);border-radius:5px;overflow:hidden;margin:10px 0 4px}
  .fan-speed-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:5px;transition:width .4s}
  .fan-pct-line{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px}
  .fan-pct-line b{color:var(--text)}
  .fan-card .fan-slider{margin-top:4px}
  .fan-card-actions{display:flex;align-items:center;flex-wrap:wrap;gap:8px 10px;margin-top:8px}
  .fan-custom{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)}
  .fan-custom-input{width:56px;padding:5px 6px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}
  .fan-custom-input:focus{outline:none;border-color:var(--blue)}
  .fan-card-state{font-size:12px;color:var(--muted)}
  /* 硬盘温度控制风扇面板 */
  .dt-panel{--dt-gap:16px;padding:18px}
  .dt-panel h3{display:flex;align-items:center;gap:10px;margin:0 0 16px;font-size:16px}
  .dt-section{margin-bottom:18px}
  .dt-section-title{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:8px}
  .dt-section-title .count{font-size:11px;color:var(--blue);background:var(--bg);padding:2px 8px;border-radius:10px;border:1px solid var(--border)}
  .dt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
  .dt-opt{display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--bg);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:.15s}
  .dt-opt:hover{border-color:var(--blue)}
  .dt-opt input[type=checkbox]{margin:0;accent-color:var(--blue);flex-shrink:0}
  .dt-opt .dt-name{font-size:13px;font-weight:500;color:var(--text);flex:1}
  .dt-opt .dt-meta{font-size:11px;color:var(--muted);white-space:nowrap}
  .dt-empty{padding:18px;text-align:center;color:var(--muted);font-size:13px;background:var(--bg);border:1px dashed var(--border);border-radius:8px}
  .dt-thresholds{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
  .dt-field{display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px}
  .dt-field label{font-size:13px;color:var(--muted);margin:0;min-width:64px}
  .dt-field input[type=number]{width:64px;padding:5px 7px;font-size:14px;text-align:center;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text)}
  .dt-field input[type=number]:focus{outline:none;border-color:var(--blue)}
  .dt-field .unit{font-size:12px;color:var(--muted)}
  .dt-options{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
  .dt-actions{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:8px}
  .dt-save-state{font-size:13px;color:var(--muted)}
  .dt-save-state.ok{color:var(--green)}
  .dt-live{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:8px;font-size:13px}
  .dt-live b{font-size:16px;color:var(--green)}
  .dt-toggle{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:500;cursor:pointer}
  .dt-toggle input{accent-color:var(--blue);flex-shrink:0}
  /* 检测新版本横幅 */
  .update-banner{display:none;align-items:center;justify-content:space-between;gap:14px;background:linear-gradient(135deg,#ea580c,#f59e0b);color:#fff;padding:11px 18px;border-radius:10px;margin-bottom:16px;box-shadow:0 2px 10px rgba(234,88,12,.28);font-size:14px}
  .update-banner.show{display:flex}
  .update-banner a{color:#fff;text-decoration:underline;font-weight:700;white-space:nowrap}
  .update-banner .ub-close{background:rgba(255,255,255,.22);border:none;color:#fff;cursor:pointer;border-radius:6px;padding:3px 11px;font-size:13px}
  .update-banner .ub-close:hover{background:rgba(255,255,255,.35)}
</style>
</head>
<body>
<div class="header">
  <h1>🖥️ 飞牛 NAS 硬件监控</h1>
  <div class="meta">
    <span id="lastUpdate">加载中…</span>
    <span><span class="spinner" id="spin"></span></span>
    <label class="switch"><input type="checkbox" id="autoRefresh"><span class="slider"></span>自动刷新</label>
    <button class="btn" onclick="loadData()">🔄 刷新</button>
    <button class="btn" id="checkUpdateBtn" onclick="checkUpdate(true)">🔔 检查更新</button>
  </div>
</div>

<div class="container">
  <aside class="sidebar">
    <div class="sidebar-title">监控模块</div>
    <div class="tabs">
      <button class="tab active" onclick="switchTab('detect',this)">🔧 硬件配置检测</button>
      <button class="tab" onclick="switchTab('raid',this)">💾 阵列卡</button>
      <button class="tab" onclick="switchTab('disks',this)">💿 硬盘 SMART</button>
      <button class="tab" onclick="switchTab('system',this)">📊 系统资源</button>
      <button class="tab" onclick="switchTab('fan',this)">🌀 风扇控制</button>
      <button class="tab" onclick="switchTab('storage',this)">🗄️ 存储卷</button>
      <button class="tab" onclick="switchTab('docker',this)">🐳 Docker</button>
    </div>
  </aside>
  <main class="content">
    <div id="updateBanner" class="update-banner">
      <div>🔔 <span id="updateText"></span></div>
      <div style="display:flex;gap:12px;align-items:center">
        <a id="updateLink" href="https://github.com/han951meng/nasdash/releases" target="_blank" rel="noopener">前往下载 →</a>
        <button class="ub-close" onclick="dismissUpdate()">✕</button>
      </div>
    </div>
    <div id="detect" class="panel active"><div class="loading">加载中…</div></div>
    <div id="raid" class="panel"><div class="loading">加载中…</div></div>
    <div id="disks" class="panel"><div class="loading">加载中…</div></div>
    <div id="system" class="panel"><div class="loading">加载中…</div></div>
    <div id="fan" class="panel"><div class="loading">加载中…</div></div>
    <div id="storage" class="panel"><div class="loading">加载中…</div></div>
    <div id="docker" class="panel"><div class="loading">加载中…</div></div>
  </main>
</div>

<script>
// 统一网关适配：页面位于 /app/{appname} 路径下时，自动为所有绝对根路径 fetch 补上前缀；
// 本地 TCP 模式（根路径）下前缀为空，行为完全不变。无需逐个改 fetch 调用。
(function(){
  var _orig = window.fetch ? window.fetch.bind(window) : null;
  if (!_orig) return;
  var base = (location.pathname || '/').replace(/\/[^\/]*$/, '');
  window.fetch = function(u, o){
    if (typeof u === 'string' && u.charAt(0) === '/' && u.charAt(1) !== '/') {
      u = base + u;
    }
    return _orig(u, o);
  };
})();
let DATA=null, timer=null;
function switchTab(id, btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(id).classList.add('active');
}
function badge(ok, okText='正常', badText='异常'){
  return `<span class="badge ${ok?'b-ok':'b-bad'}">${ok?okText:badText}</span>`;
}
function tempColor(t, trip){
  if(t==null) return 'var(--muted)';
  let pct = trip? t/trip : t/60;
  if(pct>0.9) return 'var(--red)';
  if(pct>0.75) return 'var(--orange)';
  return 'var(--green)';
}
function fmtHours(h){
  if(h==null) return '-';
  if(h>=8760) return (h/8760).toFixed(1)+' 年';
  return h+' 小时';
}

function renderRaid(r, disks){
  // 纯 SATA 主板：无独立阵列卡 / HBA
  if(r.mode === 'none'){
    return '<div class="card"><div class="loading">'+(r.note||'未检测到阵列卡')+'</div></div>';
  }
  // MegaRAID 卡存在但 storcli 读取失败
  if(r.mode === 'mega_error'){
    return '<div class="card"><div class="loading" style="color:var(--red)">'+(r.note||'读取失败')+'</div></div>';
  }
  // HBA 直通卡（IT 模式）：盘由系统直接管理，内联 smartctl 直读的磁盘温度
  if(r.mode === 'hba'){
    let diskRows = (disks && disks.length) ? disks.map(d=>{
      let trip = d.temp_trip || 60;
      let tstr = d.temp != null ? d.temp + '°C' : 'N/A';
      let tcol = tempColor(d.temp, trip);
      let mdl = (d.vendor ? d.vendor + ' ' : '') + (d.model || '');
      let typ = d.type === 'nvme' ? 'NVMe' : (d.type === 'sas' ? 'SAS' : (d.rota === '1' ? 'SATA HDD' : 'SATA SSD'));
      return `<tr><td>${d.dev}</td><td>${mdl}</td><td>${typ}</td><td style="color:${tcol};font-weight:600">${tstr}</td></tr>`;
    }).join('') : '<tr><td colspan=4>未检测到磁盘（请用 smartctl 确认盘已被系统识别）</td></tr>';
    return `
    <div class="cards">
      <div class="card">
        <h3>控制器 (HBA 直通)</h3>
        <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
        <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info">IT 直通</span></span></div>
        <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">✓ 正常工作</span></div>
        ${r.controller_temp != null ? `<div class="kv"><span class="k">芯片温度</span><span class="v" style="color:${tempColor(r.controller_temp,85)};font-weight:600">${r.controller_temp}°C</span></div>` : ''}
      </div>
      <div class="card" style="grid-column: span 2">
        <h3>说明</h3>
        <div style="margin-top:6px;color:var(--muted);font-size:13px;line-height:1.7">${r.note}</div>
      </div>
    </div>
    <div class="section-title">已识别磁盘温度（smartctl 直读，无需阵列卡）</div>
    <div class="card">
      <table class="table"><thead><tr><th>设备</th><th>型号</th><th>类型</th><th>温度</th></tr></thead><tbody>${diskRows}</tbody></table>
    </div>`;
  }
  // MegaRAID (IR 模式)：现有展示逻辑
  let ct = r.controller_temp;
  let ctStr = ct != null ? ct + '°C' : 'N/A';
  let ctCol = tempColor(ct, 85);
  let drives = r.drives.map(d=>`<tr><td>${d.slot}</td><td>${d.brand||'-'}</td><td>${d.model}${d.feature?` <span class="badge b-info">${d.feature}</span>`:''}</td><td>${d.intf} ${d.media}</td><td>${d.size}${d.size_note?`<br><span style="font-size:11px;color:var(--muted)">${d.size_note}</span>`:''}</td><td><span class="badge ${d.state==='JBOD'||d.state==='Onln'?'b-ok':'b-warn'}">${d.state}${d.sp==='D'?' · 已停转':''}</span></td><td>${d.rpm||'-'}</td></tr>`).join('');
  return `
  <div class="cards">
    <div class="card">
      <h3>阵列卡信息</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
      <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info">MegaRAID (IR)</span></span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">✓ 正常工作</span></div>
      <div class="kv"><span class="k">序列号</span><span class="v">${r.serial}</span></div>
      <div class="kv"><span class="k">SAS 地址</span><span class="v">${r.sas_address}</span></div>
      <div class="kv"><span class="k">PCI 地址</span><span class="v">${r.pci}</span></div>
      <div class="kv"><span class="k">芯片温度</span><span class="v" style="color:${ctCol};font-weight:600">${ctStr}</span></div>
    </div>
    <div class="card">
      <h3>固件版本</h3>
      <div class="kv"><span class="k">FW Package</span><span class="v">${r.fw_package}</span></div>
      <div class="kv"><span class="k">FW Version</span><span class="v">${r.fw_version}</span></div>
      <div class="kv"><span class="k">BIOS</span><span class="v">${r.bios_version}</span></div>
      <div class="kv"><span class="k">驱动</span><span class="v">${r.driver}</span></div>
    </div>
    <div class="card">
      <h3>缓存 / 电池</h3>
      <div class="kv"><span class="k">CacheVault</span><span class="v">${r.cachevault}</span></div>
      <div class="kv"><span class="k">JBOD 盘数</span><span class="v">${r.jbod_count}</span></div>
    </div>
    <div class="card" style="grid-column: 1 / -1">
      <h3>说明</h3>
      <div style="margin-top:6px;color:var(--muted);font-size:13px;line-height:1.7">MegaRAID (IR 模式)：磁盘由阵列卡固件统一管理，支持 RAID 0/1/5/10 与 JBOD。阵列卡芯片温度由 storcli 读取（ROC temperature）；每张物理盘的状态与转速见下方列表，单盘 SMART 温度与健康度请见「硬盘 SMART」标签页。</div>
    </div>
  </div>
  <div class="section-title">物理盘列表</div>
  <div class="card">
    <div style="overflow-x:auto"><table class="table"><thead><tr><th>槽位</th><th>品牌</th><th>型号</th><th>接口</th><th>容量</th><th>状态</th><th>转速</th></tr></thead><tbody>${drives||'<tr><td colspan=7>无</td></tr>'}</tbody></table></div>
  </div>`;
}

function renderDisks(disks){
  if(!disks||!disks.length) return '<div class="card"><div class="loading">未检测到硬盘</div></div>';
  return '<div class="cards">'+disks.map(d=>{
    let isSAS = d.type==='sas';
    let isNVMe = d.type==='nvme';
    let healthBadge = d.health_ok ? '<span class="badge b-ok">'+d.health+'</span>' : '<span class="badge b-bad">'+d.health+'</span>';
    let cls = d.health_ok ? '' : 'bad';
    let trip = d.temp_trip||60;
    let tempPct = d.temp!=null ? Math.min(d.temp/trip*100,100) : 0;
    let tempStr = d.temp!=null ? d.temp+'°C' : 'N/A';
    let detail = '';
    if(isSAS){
      detail = `
        <div class="kv"><span class="k">已用时长</span><span class="v">${fmtHours(d.power_on_hours)}</span></div>
        <div class="kv"><span class="k">缺陷扇区</span><span class="v" style="color:${d.defects>0?'var(--red)':'var(--text)'}">${d.defects}</span></div>
        <div class="kv"><span class="k">待处理缺陷</span><span class="v" style="color:${d.pending>0?'var(--orange)':'var(--text)'}">${d.pending}</span></div>
        <div class="kv"><span class="k">非介质错误</span><span class="v">${d.non_medium_errors||0}</span></div>
        <div class="kv"><span class="k">读错误(不可纠正)</span><span class="v">${d.read_errors}</span></div>
        <div class="kv"><span class="k">写错误(不可纠正)</span><span class="v">${d.write_errors}</span></div>`;
    } else if(isNVMe) {
      detail = `
        <div class="kv"><span class="k">已用时长</span><span class="v">${fmtHours(d.power_on_hours)}</span></div>
        <div class="kv"><span class="k">已用寿命</span><span class="v" style="color:${d.percentage_used>0?'var(--orange)':'var(--text)'}">${d.percentage_used!=null?d.percentage_used+'%':'N/A'}</span></div>
        <div class="kv"><span class="k">剩余备用</span><span class="v">${d.available_spare!=null?d.available_spare+'%':'N/A'}</span></div>
        <div class="kv"><span class="k">临界告警</span><span class="v" style="color:${d.critical_warning&&d.critical_warning!=='0'?'var(--red)':'var(--text)'}">0x${d.critical_warning||'0'}</span></div>
        <div class="kv"><span class="k">读取量</span><span class="v">${d.data_units_read||'N/A'}</span></div>
        <div class="kv"><span class="k">写入量</span><span class="v">${d.data_units_written||'N/A'}</span></div>`;
    } else {
      detail = `
        <div class="kv"><span class="k">已用时长</span><span class="v">${fmtHours(d.power_on_hours)}</span></div>
        <div class="kv"><span class="k">重映射扇区</span><span class="v" style="color:${d.reallocated>0?'var(--red)':'var(--text)'}">${d.reallocated}</span></div>
        <div class="kv"><span class="k">待处理扇区</span><span class="v" style="color:${d.pending>0?'var(--orange)':'var(--text)'}">${d.pending}</span></div>
        <div class="kv"><span class="k">不可纠正扇区</span><span class="v" style="color:${d.uncorrectable>0?'var(--red)':'var(--text)'}">${d.uncorrectable}</span></div>
        <div class="kv"><span class="k">UDMA CRC 错误</span><span class="v">${d.udma_crc||0}</span></div>`;
    }
    return `<div class="card disk-card ${cls}">
      <div class="disk-head">
        <span class="name">${d.dev}</span>
        ${healthBadge}
      </div>
      <div class="kv"><span class="k">型号</span><span class="v">${d.brand?d.brand+' ':''}${d.vendor?d.vendor+' ':''}${d.model}</span></div>
      <div class="kv"><span class="k">容量 / 类型</span><span class="v">${d.size} · ${isNVMe?'NVMe':(isSAS?'SAS':'SATA')} · ${d.rota=='1'?'HDD':'SSD'}${d.feature?' · '+d.feature:''}</span></div>
      <div class="kv"><span class="k">转速</span><span class="v">${d.rpm||(d.rota=='1'||isSAS?'—':'固态(SSD)')}</span></div>
      <div class="kv"><span class="k">序列号</span><span class="v" style="font-size:12px">${d.serial}</span></div>
      <div style="margin-top:8px"><span style="font-size:13px;color:var(--muted)">温度 ${tempStr}</span><div class="temp-bar"><div class="temp-fill" style="width:${tempPct}%;background:${tempColor(d.temp,trip)}"></div></div></div>
      ${detail}
    </div>`;
  }).join('')+'</div>';
}

function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function renderSystem(s){
  let memPct = s.memory.percent;
  let memColor = memPct>85?'var(--red)':memPct>70?'var(--orange)':'var(--green)';
  let sens = s.sensors || {};
  let tempsHtml = (sens.temps||[]).map(t=>{
    let col = t.crit&&t.value>=t.crit ? 'var(--red)' : t.max&&t.value>=t.max ? 'var(--orange)' : 'inherit';
    let extra = t.crit ? ` (上限${t.max||'-'}°C/临界${t.crit}°C)` : t.max ? ` (上限${t.max}°C)` : '';
    return `<div class="kv"><span class="k">${t.name}</span><span class="v" style="color:${col}">${t.value}°C${extra}</span></div>`;
  }).join('');
  let fansHtml = (sens.fans||[]).map(f=>{
    let stopped = f.stopped || f.rpm < 1;
    let color = stopped ? 'var(--red)' : 'inherit';
    let rpmStr = stopped ? '0 RPM (停转)' : f.rpm + ' RPM';
    let modeMap = {'curve':'曲线温控','manual':'手动控制','auto':'自动温控','off':'关闭'};
    let modeStr = f.mode ? ` <span class="pill ${f.mode==='curve'||f.mode==='auto'?'b-info':'b-warn'}">${modeMap[f.mode]||f.mode}</span>` : '';
    let voltBadge = f.voltage ? ` <span class="volt-badge">${esc(f.voltage)}</span>` : '';
    let pwmStr = (f.pwm!=null) ? ` · PWM ${f.pwm}%` : '';
    let fanName = esc(f.label || f.name);
    return `<div class="kv"><span class="k">${fanName}${voltBadge}${modeStr}</span><span class="v" style="color:${color}">${rpmStr}${pwmStr}</span></div>`;
  }).join('');
  let voltsHtml = (sens.voltages||[]).map(v=>`<div class="kv"><span class="k">${v.name}</span><span class="v">${v.value} V</span></div>`).join('');
  let loadColor = (l)=>parseFloat(l)>s.cpu_threads?'var(--red)':parseFloat(l)>s.cpu_threads*0.7?'var(--orange)':'var(--green)';
  return `
  <div class="grid-2">
    <div class="card">
      <h3>负载</h3>
      <div class="kv"><span class="k">1 分钟</span><span class="v" style="color:${loadColor(s.load[0])}">${s.load[0]}</span></div>
      <div class="kv"><span class="k">5 分钟</span><span class="v" style="color:${loadColor(s.load[1])}">${s.load[1]}</span></div>
      <div class="kv"><span class="k">15 分钟</span><span class="v" style="color:${loadColor(s.load[2])}">${s.load[2]}</span></div>
      <div style="font-size:12px;color:var(--muted);margin-top:6px">阈值：> ${s.cpu_threads} 为过载</div>
    </div>
    <div class="card">
      <h3>内存使用</h3>
      <div class="kv"><span class="k">总量</span><span class="v">${s.memory.total}</span></div>
      <div class="kv"><span class="k">已用</span><span class="v">${s.memory.used}</span></div>
      <div class="kv"><span class="k">可用</span><span class="v">${s.memory.available}</span></div>
      <div style="margin-top:8px"><div class="progress"><div class="progress-fill" style="width:${memPct}%;background:${memColor}">${memPct}%</div></div></div>
    </div>
  </div>
  <div class="grid-auto" style="margin-top:14px">
    <div class="card">
      <h3>温度传感器</h3>
      ${tempsHtml||'<div class="loading">无数据</div>'}
    </div>
    <div class="card">
      <h3>风扇转速</h3>
      ${fansHtml||'<div class="loading">无数据</div>'}
    </div>
    <div class="card">
      <h3>电压</h3>
      ${voltsHtml||'<div class="loading">无数据</div>'}
    </div>
  </div>`;
}

function renderFanControls(fans){
  let ctrls = (fans||[]).filter(f=>f.controllable).map(f=>{
    let id='fan'+f.idx;
    let pct=(f.pwm!=null)?f.pwm:50;
    return `<div class="fan-ctrl" id="${id}-box">
      <div class="fan-ctrl-head"><span>${f.name}</span><span class="fan-ctrl-val"><span id="${id}-cur">${pct}</span>% <span class="fan-ctrl-tgt" id="${id}-tgt"></span></span></div>
      <input type="range" min="30" max="100" value="${pct}" class="fan-slider" data-hwmon="${f.hwmon}" data-idx="${f.idx}" id="${id}-slider">
      <div class="fan-ctrl-actions">
        <span class="fan-ctrl-rpm">转速 <b id="${id}-rpm">${f.rpm||0}</b> RPM</span>
        <button class="btn-mini" onclick="setFanAuto('${f.hwmon}', ${f.idx})">恢复自动</button>
        <span class="fan-ctrl-state" id="${id}-state"></span>
      </div>
    </div>`;
  }).join('');
  if(!ctrls) return `<div class="loading">未检测到可调控风扇（部分 NAS 由系统固件统一控温，本工具不接管）</div>`;
  return `<div class="fan-ctrl-list">${ctrls}</div>`;
}

// ===== 风扇控制独立页面（预设档位 + 实时转速卡片）=====
let FAN_LIST=[];
let SHOW_HIDDEN_FANS=false;
function toggleShowHiddenFans(){ SHOW_HIDDEN_FANS=!SHOW_HIDDEN_FANS; if(typeof loadData==='function'){ loadData(); } }
function renderFanControl(){
  let allFans=(DATA.system&&DATA.system.sensors&&DATA.system.sensors.fans||[]).filter(f=>f.controllable);
  let hiddenCount=allFans.filter(f=>f.hidden).length;
  let fans=SHOW_HIDDEN_FANS?allFans:allFans.filter(f=>!f.hidden);
  FAN_LIST=fans;
  if(!allFans.length) return '<div class="card"><div class="loading">未检测到可调控风扇（部分 NAS 由系统固件统一控温，本工具不接管）</div></div>';
  let presets=[['auto','默认'],['100','全速'],['10','10%'],['20','20%'],['30','30%'],['40','40%'],['50','50%'],['60','60%'],['70','70%'],['80','80%'],['90','90%']];
  let presetHtml=presets.map(p=>`<button class="preset-btn ${p[0]==='100'?'full':''}" data-preset="${p[0]}" onclick="applyFanPreset('${p[0]}')">${p[1]}</button>`).join('');
  let modeMap={'curve':'曲线温控','manual':'手动控制','auto':'自动温控','off':'关闭','disk_temp':'硬盘温控','sys_temp':'主板/CPU温控'};
  let cards=fans.map(f=>{
    let id='fan'+f.idx;
    let pct=(f.pwm!=null)?f.pwm:50;
    return `<div class="fan-card" id="${id}-box">
      <div class="fan-card-head">
        <input class="fan-label-input" id="${id}-label" value="${esc(f.label||f.name||'')}" placeholder="自定义名称（如 CPU 风扇）" maxlength="40">
        <span class="pill ${f.mode==='curve'||f.mode==='auto'?'b-info':'b-warn'}">${modeMap[f.mode]||f.mode||''}</span>
        <span class="fan-label-save">
          <select class="fan-volt-select" id="${id}-volt" title="风扇供电电压">
            <option value="12V" ${f.voltage==='12V'?'selected':''}>12V</option>
            <option value="5V" ${f.voltage==='5V'?'selected':''}>5V</option>
            <option value="未知" ${(!f.voltage||f.voltage==='未知')?'selected':''}>未知</option>
          </select>
          <button class="btn-mini" onclick="saveFanLabel('${f.hwmon}', ${f.idx})">保存标注</button>
          <span id="${id}-label-state" class="fan-label-state"></span>
        </span>
      </div>
      <div class="fan-rpm" ${f.has_tach?'':'title="读不到转速：可能是一分二分线器的副风扇（转速线未接）、风扇本身未接转速线，或主板未把该通道布线到风扇接口。若此通道无风扇，可点下方『隐藏』把它收起。"'}><span id="${id}-rpm">${f.has_tach?(f.rpm||0):'—'}</span><small id="${id}-rpm-unit">${f.has_tach?'RPM':'无转速信号'}</small></div>
      <div class="fan-speed-bar"><div class="fan-speed-fill" id="${id}-bar" style="width:${pct}%"></div></div>
      <div class="fan-pct-line">
        <span>当前 <b id="${id}-cur">${pct}</b>%</span>
        <span>目标 <b id="${id}-tgt"></b></span>
      </div>
      <input type="range" min="10" max="100" value="${pct}" class="fan-slider" data-hwmon="${f.hwmon}" data-idx="${f.idx}" id="${id}-slider">
      <div class="fan-card-actions">
        <button class="btn-mini" onclick="setFanAuto('${f.hwmon}', ${f.idx})">恢复自动</button>
        <span class="fan-custom">
          <input type="number" min="10" max="100" step="1" value="${pct}" class="fan-custom-input" id="${id}-custom" data-hwmon="${f.hwmon}" data-idx="${f.idx}" title="自定义该风扇占空比"> %
          <button class="btn-mini" onclick="applyFanCustom('${f.hwmon}', ${f.idx})">应用</button>
        </span>
        <button class="btn-mini" onclick="toggleFanHidden('${f.hwmon}', ${f.idx}, ${f.hidden?'false':'true'})" title="隐藏无风扇/无转速的空通道，可随时恢复">${f.hidden?'取消隐藏':'隐藏'}</button>
        <span class="fan-card-state" id="${id}-state"></span>
      </div>
    </div>`;
  }).join('');
  let dtHtml = renderDiskTempPanel();
  let stHtml = renderSysTempPanel();
  let hideToggle = hiddenCount>0 ? `<button class="btn-mini" style="margin-left:8px" onclick="toggleShowHiddenFans()">${SHOW_HIDDEN_FANS?('收起已隐藏('+hiddenCount+')'):('显示已隐藏通道('+hiddenCount+')')}</button>` : '';
  let gridInner = cards || `<div class="loading">可见风扇均已隐藏${hiddenCount>0?'，点上方「显示已隐藏通道」可展开':''}。</div>`;
  return `<div class="fan-page">
    ${dtHtml}
    ${stHtml}
    <div class="fan-presets">
      <span class="label">一键调速：</span>${presetHtml}
      ${hideToggle}
      <span class="fan-global-state" id="fan-global-state"></span>
    </div>
    <div class="fan-grid">${gridInner}</div>
  </div>`;
}
async function applyFanPreset(val){
  let gs=document.getElementById('fan-global-state');
  if(!FAN_LIST.length) return;
  if(val==='auto'){
    if(gs) gs.textContent='正在恢复自动控温…';
    for(let f of FAN_LIST) await setFanAuto(f.hwmon, f.idx);
    if(gs) gs.textContent='已全部交还自动控温';
  }else{
    let pwm=parseInt(val,10);
    if(gs) gs.textContent='正在设为 '+pwm+'%…';
    for(let f of FAN_LIST) await applyFan(f.hwmon, f.idx, pwm);
    if(gs) gs.textContent='已全部设为 '+pwm+'%';
  }
  highlightPreset(val);
}
let fanTimer=null;
function clearPresetHighlight(){
  document.querySelectorAll('.preset-btn[data-preset]').forEach(b=>b.classList.remove('active'));
}
function highlightPreset(val){
  clearPresetHighlight();
  let b=document.querySelector('.preset-btn[data-preset="'+val+'"]');
  if(b) b.classList.add('active');
}
// 根据各卡真实目标/模式，高亮当前统一生效的档位（无统一档位则不高亮）
function updatePresetHighlight(fans){
  clearPresetHighlight();
  if(!fans || !fans.length) return;
  let manualFans=fans.filter(f=>f.mode!=='auto' && f.mode!=='off');
  if(manualFans.length===0){
    let b=document.querySelector('.preset-btn[data-preset="auto"]');
    if(b) b.classList.add('active');
    return;
  }
  let targets=manualFans.map(f=>(f.target_pct!=null?f.target_pct:f.pwm)).filter(p=>p!=null);
  if(targets.length===manualFans.length && targets.every(t=>t===targets[0])){
    let b=document.querySelector('.preset-btn[data-preset="'+targets[0]+'"]');
    if(b) b.classList.add('active');
  }
}
async function applyFanCustom(hwmon, idx){
  let id='fan'+idx;
  let inp=document.getElementById(id+'-custom');
  if(!inp) return;
  let v=parseInt(inp.value,10);
  if(isNaN(v)) return;
  v=Math.max(10,Math.min(100,v));
  inp.value=v;
  await applyFan(hwmon, idx, v);
}
async function fetchFanStatus(){
  try{
    let r=await fetch('/api/fan/status?_='+Date.now());
    let j=await r.json();
    (j.fans||[]).forEach(f=>{
      let id='fan'+f.idx;
      let cur=document.getElementById(id+'-cur');
      let tgt=document.getElementById(id+'-tgt');
      let rpm=document.getElementById(id+'-rpm');
      let sl=document.getElementById(id+'-slider');
      if(cur) cur.textContent=(f.pwm!=null?f.pwm:'--');
      if(tgt) tgt.textContent=(f.target_pct!=null && f.target_pct!==f.pwm)?('→ 目标 '+f.target_pct+'%'):(f.mode==='auto'?'· 自动控温':'');
      if(rpm) rpm.textContent=(f.has_tach?(f.rpm||0):'—');
      let unit=document.getElementById(id+'-rpm-unit'); if(unit) unit.textContent=(f.has_tach?'RPM':'无转速信号');
      let bar=document.getElementById(id+'-bar');
      if(bar) bar.style.width=(f.pwm!=null?f.pwm:50)+'%';
      if(sl && document.activeElement!==sl){
        // 手动模式下滑块停在用户设定的目标值，不强行跟随实时爬升（避免松手后滑块回弹误以为没生效）；自动模式跟随实时值
        sl.value=(f.mode==='manual' && f.target_pct!=null)?f.target_pct:(f.pwm!=null?f.pwm:50);
      }
    });
    updatePresetHighlight(j.fans);
  }catch(e){}
}
async function applyFan(hwmon, idx, pwm){
  let id='fan'+idx, st=document.getElementById(id+'-state');
  if(st) st.textContent='目标 '+pwm+'%，平滑过渡中…';
  try{
    let r=await fetch('/api/fan/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hwmon:hwmon,idx:idx,pwm:pwm})});
    let j=await r.json();
    if(st) st.textContent=j.ok?('目标 '+pwm+'%，平滑过渡中'):('失败：'+(j.error||''));
  }catch(e){ if(st) st.textContent='请求失败'; }
}
async function setFanAuto(hwmon, idx){
  let id='fan'+idx, st=document.getElementById(id+'-state');
  if(st) st.textContent='恢复中…';
  try{
    let r=await fetch('/api/fan/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hwmon:hwmon,idx:idx,mode:'auto'})});
    let j=await r.json();
    let owner=j.owner==='ext_service'?'（已交还系统风扇服务）':'（nasdash 接管温控）';
    if(st) st.textContent=j.ok?('已恢复自动 '+owner):('失败：'+(j.error||''));
  }catch(e){ if(st) st.textContent='请求失败'; }
}
async function saveFanLabel(hwmon, idx){
  let id='fan'+idx;
  let labInp=document.getElementById(id+'-label');
  let voltSel=document.getElementById(id+'-volt');
  let st=document.getElementById(id+'-label-state');
  if(!labInp||!voltSel) return;
  try{
    let r=await fetch('/api/fan/labels'); let cur=await r.json();
    let k=hwmon+'::'+idx; let e=cur[k]||{};
    e.name=labInp.value.trim(); e.voltage=voltSel.value;  // 保留 e.hidden 不被覆盖
    cur[k]=e;
    let r2=await fetch('/api/fan/labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cur)});
    let j=await r2.json();
    if(st) st.textContent=j.ok?'已保存 ✓':'失败';
    if(j.ok){ loadData(); }
  }catch(e){ if(st) st.textContent='请求失败'; }
}
async function toggleFanHidden(hwmon, idx, hide){
  try{
    let r=await fetch('/api/fan/labels'); let cur=await r.json();
    let k=hwmon+'::'+idx; let e=cur[k]||{};
    e.hidden=!!hide; cur[k]=e;
    await fetch('/api/fan/labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cur)});
    if(!hide){ SHOW_HIDDEN_FANS=true; }  // 取消隐藏后确保该风扇仍可见
    if(typeof loadData==='function'){ loadData(); }
  }catch(e){}
}
function bindFanSliders(){
  document.querySelectorAll('.fan-slider').forEach(sl=>{
    sl.addEventListener('input', ()=>{
      clearPresetHighlight();
      let id='fan'+sl.dataset.idx;
      let v=sl.value;
      let cur=document.getElementById(id+'-cur'); if(cur) cur.textContent=v;
      let tgt=document.getElementById(id+'-tgt'); if(tgt) tgt.textContent='→ 目标 '+v+'%';
      clearTimeout(sl._t);
      sl._t=setTimeout(()=>applyFan(sl.dataset.hwmon, parseInt(sl.dataset.idx), parseInt(sl.value)), 120);
    });
  });
  document.querySelectorAll('.fan-custom-input').forEach(inp=>{
    inp.addEventListener('keydown', e=>{ if(e.key==='Enter'){ e.preventDefault(); applyFanCustom(inp.dataset.hwmon, parseInt(inp.dataset.idx)); } });
  });
  if(!fanTimer){ fetchFanStatus(); fanTimer=setInterval(fetchFanStatus, 2000); }
}

// ===== 硬盘温度控制风扇（disk_temp）=====
function renderDiskTempPanel(){
  var fans=(typeof FAN_LIST!=='undefined'?FAN_LIST:[])||[];
  var disks=(DATA&&DATA.disks)||[];
  var fanOptsHtml='', diskOptsHtml='';
  if(fans.length){
    fanOptsHtml=fans.map(function(f){
      var id='dt-fan-'+f.hwmon+'-'+f.idx;
      var label=esc((f.label||f.name||('风扇'+f.idx))).replace(/</g,'&lt;');
      var meta=(f.rpm||0)+' RPM';
      return '<label class="dt-opt" for="'+id+'"><input type="checkbox" class="dt-fan" id="'+id+'" data-hwmon="'+f.hwmon+'" data-idx="'+f.idx+'"> <span class="dt-name">'+label+'</span><span class="dt-meta">'+meta+'</span></label>';
    }).join('');
  }else{
    fanOptsHtml='<div class="dt-empty">未检测到可调控风扇</div>';
  }
  if(disks.length){
    diskOptsHtml=disks.map(function(d){
      var shortDev=(d.dev||'').replace(/^\/dev\//,'');
      var id='dt-disk-'+shortDev;
      var name=esc(shortDev);
      var meta='';
      if(d.model||d.vendor) meta=esc((d.vendor||'')+' '+(d.model||'')).trim();
      return '<label class="dt-opt" for="'+id+'"><input type="checkbox" class="dt-disk" id="'+id+'" value="'+d.dev+'"> <span class="dt-name">'+name+'</span><span class="dt-meta" id="dt-temp-'+shortDev+'">'+(meta?meta:'')+'</span></label>';
    }).join('');
  }else{
    diskOptsHtml='<div class="dt-empty">未检测到硬盘</div>';
  }
  return '<div class="card dt-panel" id="disk-temp-panel">'
    +'<h3>硬盘温度控制风扇 <span class="badge b-info">新功能</span></h3>'
    +'<label class="dt-toggle" style="margin-bottom:14px"><input type="checkbox" id="dt-enabled"> 启用硬盘温控</label>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">监控硬盘 <span class="count" id="dt-disk-count">0</span></div>'
      +'<div class="dt-grid">'+diskOptsHtml+'</div>'
    +'</div>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">受控风扇 <span class="count" id="dt-fan-count">0</span></div>'
      +'<div class="dt-grid">'+fanOptsHtml+'</div>'
    +'</div>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">温控曲线</div>'
      +'<div class="dt-thresholds">'
        +'<div class="dt-field"><label>开转温度</label><input type="number" id="dt-start" min="20" max="80" value="40"><span class="unit">°C</span></div>'
        +'<div class="dt-field"><label>全速温度</label><input type="number" id="dt-full" min="30" max="90" value="60"><span class="unit">°C</span></div>'
        +'<div class="dt-field"><label>开转占空比</label><input type="number" id="dt-min" min="0" max="100" value="30"><span class="unit">%</span></div>'
        +'<div class="dt-field"><label>全速占空比</label><input type="number" id="dt-max" min="0" max="100" value="100"><span class="unit">%</span></div>'
        +'<div class="dt-field"><label>恢复自动温度</label><input type="number" id="dt-recover" min="20" max="80" value="35"><span class="unit">°C</span></div>'
      +'</div>'
    +'</div>'
    +'<div class="dt-section">'
      +'<div class="dt-options">'
        +'<label class="dt-toggle"><input type="checkbox" id="dt-sleep" checked> 监控盘全部休眠时停转风扇</label>'
      +'</div>'
    +'</div>'
    +'<div class="dt-actions">'
      +'<button class="btn-mini" id="dt-save" onclick="saveDiskTempConfig()">保存配置</button>'
      +'<span class="dt-live">当前计算目标占空比：<b id="dt-live-pwm">—</b></span>'
      +'<span class="dt-save-state" id="dt-save-state"></span>'
    +'</div>'
  +'</div>';
}
function updateDiskTempCounts(){
  var dc=document.querySelectorAll('.dt-disk:checked').length;
  var fc=document.querySelectorAll('.dt-fan:checked').length;
  var dce=document.getElementById('dt-disk-count'); if(dce) dce.textContent=dc;
  var fce=document.getElementById('dt-fan-count'); if(fce) fce.textContent=fc;
}
var dtTimer=null;
async function initDiskTemp(){
  try{
    var r=await fetch('/api/fan/disk_temp?_='+Date.now());
    var j=await r.json();
    var c=j.config||{};
    var en=document.getElementById('dt-enabled'); if(en) en.checked=!!c.enabled;
    var st=document.getElementById('dt-start'); if(st) st.value=(c.start_temp!=null?c.start_temp:40);
    var fu=document.getElementById('dt-full'); if(fu) fu.value=(c.full_temp!=null?c.full_temp:60);
    var mn=document.getElementById('dt-min'); if(mn) mn.value=(c.min_pwm!=null?c.min_pwm:30);
    var mx=document.getElementById('dt-max'); if(mx) mx.value=(c.max_pwm!=null?c.max_pwm:100);
    var rc=document.getElementById('dt-recover'); if(rc) rc.value=(c.recover_temp!=null?c.recover_temp:35);
    var sl=document.getElementById('dt-sleep'); if(sl) sl.checked=(c.sleep_stop!==false);
    (c.disks||[]).forEach(function(dev){
      var shortDev=String(dev).replace(/^\/dev\//,'');
      var cbs=document.querySelectorAll('.dt-disk');
      for(var i=0;i<cbs.length;i++){ if(cbs[i].value===shortDev){ cbs[i].checked=true; break; } }
    });
    var cf=c.controlled_fans||'all';
    var fanCbs=document.querySelectorAll('.dt-fan');
    if(cf==='all' && fanCbs.length){ fanCbs.forEach(function(cb){cb.checked=true;}); }
    else if(Array.isArray(cf)){
      fanCbs.forEach(function(cb){
        var h=cb.dataset.hwmon, idx=parseInt(cb.dataset.idx);
        cf.forEach(function(pair){ if(pair && pair[0]===h && pair[1]===idx){ cb.checked=true; } });
      });
    }
    var panel=document.getElementById('disk-temp-panel');
    if(panel){
      panel.addEventListener('change', function(e){
        if(e.target && (e.target.classList.contains('dt-disk') || e.target.classList.contains('dt-fan'))) updateDiskTempCounts();
      });
    }
    updateDiskTempCounts();
    refreshDiskTempPanel();
    if(dtTimer) clearInterval(dtTimer);
    dtTimer=setInterval(refreshDiskTempPanel,5000);
  }catch(e){}
}
async function refreshDiskTempPanel(){
  var panel=document.getElementById('disk-temp-panel');
  if(!panel) return;
  try{
    var r=await fetch('/api/fan/disk_temp?_='+Date.now());
    var j=await r.json();
    (j.disks||[]).forEach(function(d){
      var shortDev=(d.dev||'').replace(/^\/dev\//,'');
      var el=document.getElementById('dt-temp-'+shortDev);
      if(el){
        if(d.asleep) el.textContent='休眠';
        else if(d.temp!=null) el.textContent=d.temp+'°C';
        else el.textContent='N/A';
      }
    });
    var live=document.getElementById('dt-live-pwm');
    if(live){ live.textContent=(j.computed_pwm!=null?j.computed_pwm+'%':'—'); }
  }catch(e){}
}
async function saveDiskTempConfig(){
  var st=document.getElementById('dt-save-state');
  var en=document.getElementById('dt-enabled'); var enabled=en?en.checked:false;
  var devs=[].slice.call(document.querySelectorAll('.dt-disk:checked')).map(function(cb){return cb.value;});
  var fanCbs=[].slice.call(document.querySelectorAll('.dt-fan:checked'));
  var allFans=[].slice.call(document.querySelectorAll('.dt-fan'));
  var controlled_fans='all';
  if(fanCbs.length && fanCbs.length<allFans.length){
    controlled_fans=fanCbs.map(function(cb){return [cb.dataset.hwmon, parseInt(cb.dataset.idx)];});
  }
  var start=parseFloat(document.getElementById('dt-start').value);
  var full=parseFloat(document.getElementById('dt-full').value);
  var minp=parseFloat(document.getElementById('dt-min').value);
  var maxp=parseFloat(document.getElementById('dt-max').value);
  var rec=parseFloat(document.getElementById('dt-recover').value);
  var sl=document.getElementById('dt-sleep'); var sleep=sl?sl.checked:true;
  if(!devs.length){ if(st){ st.textContent='请至少勾选一个硬盘'; st.className='dt-save-state'; } return; }
  if(!(full>start)){ if(st){ st.textContent='全速温度须大于开转温度'; st.className='dt-save-state'; } return; }
  if(!(rec<start)){ if(st){ st.textContent='恢复自动温度须小于开转温度'; st.className='dt-save-state'; } return; }
  if(st){ st.textContent='保存中…'; st.className='dt-save-state'; }
  try{
    var body={enabled:enabled,disks:devs,start_temp:start,full_temp:full,min_pwm:minp,max_pwm:maxp,recover_temp:rec,sleep_stop:sleep,controlled_fans:controlled_fans};
    var r=await fetch('/api/fan/disk_temp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var j=await r.json();
    if(st){ st.textContent=j.ok?'已保存 ✓':'失败：'+(j.error||''); st.className=j.ok?'dt-save-state ok':'dt-save-state'; }
    if(j.ok){ refreshDiskTempPanel(); }
  }catch(e){ if(st){ st.textContent='请求失败'; st.className='dt-save-state'; } }
}

// ===== 主板/CPU 温度控制风扇（sys_temp）=====
function renderSysTempPanel(){
  var fans=(typeof FAN_LIST!=='undefined'?FAN_LIST:[])||[];
  var fanOptsHtml='';
  if(fans.length){
    fanOptsHtml=fans.map(function(f){
      var id='st-fan-'+f.hwmon+'-'+f.idx;
      var label=esc((f.label||f.name||('风扇'+f.idx))).replace(/</g,'&lt;');
      var meta=(f.rpm||0)+' RPM';
      return '<label class="dt-opt" for="'+id+'"><input type="checkbox" class="st-fan" id="'+id+'" data-hwmon="'+f.hwmon+'" data-idx="'+f.idx+'"> <span class="dt-name">'+label+'</span><span class="dt-meta">'+meta+'</span></label>';
    }).join('');
  }else{
    fanOptsHtml='<div class="dt-empty">未检测到可调控风扇</div>';
  }
  return '<div class="card dt-panel" id="sys-temp-panel">'
    +'<h3>主板 / CPU 温度控制风扇 <span class="badge b-info">新功能</span></h3>'
    +'<label class="dt-toggle" style="margin-bottom:14px"><input type="checkbox" id="st-enabled"> 启用主板/CPU 温控</label>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">温度来源</div>'
      +'<div class="dt-options">'
        +'<label class="dt-toggle"><input type="radio" name="st-source" value="cpu" checked> CPU 封装温度</label>'
        +'<label class="dt-toggle"><input type="radio" name="st-source" value="mb"> 主板温度</label>'
      +'</div>'
    +'</div>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">受控风扇 <span class="count" id="st-fan-count">0</span></div>'
      +'<div class="dt-grid">'+fanOptsHtml+'</div>'
    +'</div>'
    +'<div class="dt-section">'
      +'<div class="dt-section-title">温控曲线</div>'
      +'<div class="dt-thresholds">'
        +'<div class="dt-field"><label>开转温度</label><input type="number" id="st-start" min="20" max="90" value="45"><span class="unit">°C</span></div>'
        +'<div class="dt-field"><label>全速温度</label><input type="number" id="st-full" min="30" max="100" value="70"><span class="unit">°C</span></div>'
        +'<div class="dt-field"><label>开转占空比</label><input type="number" id="st-min" min="0" max="100" value="30"><span class="unit">%</span></div>'
        +'<div class="dt-field"><label>全速占空比</label><input type="number" id="st-max" min="0" max="100" value="100"><span class="unit">%</span></div>'
        +'<div class="dt-field"><label>恢复自动温度</label><input type="number" id="st-recover" min="20" max="90" value="40"><span class="unit">°C</span></div>'
      +'</div>'
    +'</div>'
    +'<div class="dt-actions">'
      +'<button class="btn-mini" id="st-save" onclick="saveSysTempConfig()">保存配置</button>'
      +'<span class="dt-live">当前<span id="st-src-label">CPU</span>温度：<b id="st-live-temp">—</b> · 计算目标占空比：<b id="st-live-pwm">—</b></span>'
      +'<span class="dt-save-state" id="st-save-state"></span>'
    +'</div>'
  +'</div>';
}
function updateSysTempCounts(){
  var fc=document.querySelectorAll('.st-fan:checked').length;
  var fce=document.getElementById('st-fan-count'); if(fce) fce.textContent=fc;
}
var stTimer=null;
async function initSysTemp(){
  try{
    var r=await fetch('/api/fan/sys_temp?_='+Date.now());
    var j=await r.json();
    var c=j.config||{};
    var en=document.getElementById('st-enabled'); if(en) en.checked=!!c.enabled;
    var cpuRb=document.querySelector('input[name="st-source"][value="cpu"]');
    var mbRb=document.querySelector('input[name="st-source"][value="mb"]');
    var src=c.source||'cpu';
    if(cpuRb) cpuRb.checked=(src==='cpu');
    if(mbRb) mbRb.checked=(src==='mb');
    var lbl=document.getElementById('st-src-label'); if(lbl) lbl.textContent=(src==='mb'?'主板':'CPU');
    var st=document.getElementById('st-start'); if(st) st.value=(c.start_temp!=null?c.start_temp:45);
    var fu=document.getElementById('st-full'); if(fu) fu.value=(c.full_temp!=null?c.full_temp:70);
    var mn=document.getElementById('st-min'); if(mn) mn.value=(c.min_pwm!=null?c.min_pwm:30);
    var mx=document.getElementById('st-max'); if(mx) mx.value=(c.max_pwm!=null?c.max_pwm:100);
    var rc=document.getElementById('st-recover'); if(rc) rc.value=(c.recover_temp!=null?c.recover_temp:40);
    var cf=c.controlled_fans||'all';
    var fanCbs=document.querySelectorAll('.st-fan');
    if(cf==='all' && fanCbs.length){ fanCbs.forEach(function(cb){cb.checked=true;}); }
    else if(Array.isArray(cf)){
      fanCbs.forEach(function(cb){
        var h=cb.dataset.hwmon, idx=parseInt(cb.dataset.idx);
        cf.forEach(function(pair){ if(pair && pair[0]===h && pair[1]===idx){ cb.checked=true; } });
      });
    }
    var panel=document.getElementById('sys-temp-panel');
    if(panel){
      panel.addEventListener('change', function(e){
        if(e.target && e.target.classList.contains('st-fan')) updateSysTempCounts();
        if(e.target && e.target.name==='st-source'){
          var l=document.getElementById('st-src-label');
          if(l) l.textContent=(e.target.value==='mb'?'主板':'CPU');
        }
      });
    }
    updateSysTempCounts();
    refreshSysTempPanel();
    if(stTimer) clearInterval(stTimer);
    stTimer=setInterval(refreshSysTempPanel,5000);
  }catch(e){}
}
async function refreshSysTempPanel(){
  var panel=document.getElementById('sys-temp-panel');
  if(!panel) return;
  try{
    var r=await fetch('/api/fan/sys_temp?_='+Date.now());
    var j=await r.json();
    var tEl=document.getElementById('st-live-temp');
    if(tEl){ tEl.textContent=(j.source_temp!=null?j.source_temp+'°C':'N/A'); }
    var live=document.getElementById('st-live-pwm');
    if(live){ live.textContent=(j.computed_pwm!=null?j.computed_pwm+'%':'—'); }
  }catch(e){}
}
async function saveSysTempConfig(){
  var st=document.getElementById('st-save-state');
  var en=document.getElementById('st-enabled'); var enabled=en?en.checked:false;
  var srcRb=document.querySelector('input[name="st-source"]:checked'); var source=srcRb?srcRb.value:'cpu';
  var fanCbs=[].slice.call(document.querySelectorAll('.st-fan:checked'));
  var allFans=[].slice.call(document.querySelectorAll('.st-fan'));
  var controlled_fans='all';
  if(fanCbs.length && fanCbs.length<allFans.length){
    controlled_fans=fanCbs.map(function(cb){return [cb.dataset.hwmon, parseInt(cb.dataset.idx)];});
  }
  var start=parseFloat(document.getElementById('st-start').value);
  var full=parseFloat(document.getElementById('st-full').value);
  var minp=parseFloat(document.getElementById('st-min').value);
  var maxp=parseFloat(document.getElementById('st-max').value);
  var rec=parseFloat(document.getElementById('st-recover').value);
  if(!(full>start)){ if(st){ st.textContent='全速温度须大于开转温度'; st.className='dt-save-state'; } return; }
  if(!(rec<start)){ if(st){ st.textContent='恢复自动温度须小于开转温度'; st.className='dt-save-state'; } return; }
  if(st){ st.textContent='保存中…'; st.className='dt-save-state'; }
  try{
    var body={enabled:enabled,source:source,start_temp:start,full_temp:full,min_pwm:minp,max_pwm:maxp,recover_temp:rec,controlled_fans:controlled_fans};
    var r=await fetch('/api/fan/sys_temp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    var j=await r.json();
    if(st){ st.textContent=j.ok?'已保存 ✓':'失败：'+(j.error||''); st.className=j.ok?'dt-save-state ok':'dt-save-state'; }
    if(j.ok){ refreshSysTempPanel(); }
  }catch(e){ if(st){ st.textContent='请求失败'; st.className='dt-save-state'; } }
}

function showBoardEdit(){ let b=document.getElementById('boardEditBox'); if(b) b.style.display='block'; }
function hideBoardEdit(){ let b=document.getElementById('boardEditBox'); if(b) b.style.display='none'; }
async function saveBoardModel(){
  let inp=document.getElementById('boardModelInput'); if(!inp) return;
  let model=inp.value.trim();
  try{
    let r=await fetch('/api/board/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:model})});
    let j=await r.json();
    if(j.ok){ hideBoardEdit(); loadData(); } else { alert('保存失败：'+(j.error||'')); }
  }catch(e){ alert('请求失败'); }
}
async function clearBoardModel(){
  try{
    let r=await fetch('/api/board/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:''})});
    let j=await r.json();
    if(j.ok){ hideBoardEdit(); loadData(); } else { alert('清除失败：'+(j.error||'')); }
  }catch(e){ alert('请求失败'); }
}

function renderStorage(st){
  let raids = (st.raid_arrays||[]).map(a=>{
    let ok = a.state==='active';
    return `<tr><td>${a.name}</td><td><span class="badge ${ok?'b-ok':'b-bad'}">${a.level}</span></td><td>${a.state}</td><td>${a.size}</td><td>${(a.disks||[]).join(', ')}</td></tr>`;
  }).join('');
  let vols = (st.volumes||[]).map(v=>{
    let pct = parseInt(v.pcent);
    let col = pct>85?'var(--red)':pct>70?'var(--orange)':'var(--green)';
    return `<tr><td>${v.mount}</td><td>${v.fstype}</td><td>${v.size}</td><td>${v.used} / ${v.avail}</td><td style="min-width:140px"><div class="progress"><div class="progress-fill" style="width:${pct}%;background:${col}">${pct}%</div></div></td></tr>`;
  }).join('');
  return `
  <div class="section-title">RAID 阵列 (mdadm)</div>
  <div class="card">
    <table class="table"><thead><tr><th>阵列</th><th>级别</th><th>状态</th><th>容量</th><th>成员盘</th></tr></thead><tbody>${raids||'<tr><td colspan=5>无</td></tr>'}</tbody></table>
  </div>
  <div class="section-title">存储卷容量</div>
  <div class="card">
    <table class="table"><thead><tr><th>挂载点</th><th>文件系统</th><th>总容量</th><th>已用/可用</th><th>使用率</th></tr></thead><tbody>${vols||'<tr><td colspan=5>无</td></tr>'}</tbody></table>
  </div>
  <div class="section-title">存储拓扑 (lsblk)</div>
  <div class="card"><pre>${(st.topology||'').replace(/</g,'&lt;')}</pre></div>`;
}

function renderDetect(D){
  if(!D) return '';
  let r = D.raid||{}, disks = D.disks||[], s = D.system||{}, st = D.storage||{}, dk = D.docker||{};
  // 状态栏健康判定
  let fans = (s.sensors&&s.sensors.fans)||[];
  let fanOk = fans.length>0 && fans.every(f=>!(f.stopped||f.rpm<1));
  let fanStr = fans.length===0 ? 'N/A' : (fanOk?'正常':'异常');
  let raidOk = r.mode==='mega'||r.mode==='hba';
  let raidStr = r.mode==='mega' ? '正常' : r.mode==='hba' ? '正常' : (r.mode==='mega_error'?'读取失败':'未配置');
  let sasDisks = disks.filter(d=>(d.type||'').toUpperCase()==='SAS');
  let sataDisks = disks.filter(d=>{let t=(d.type||'').toUpperCase(); return t==='SATA'||t==='ATA';});
  let sasHealthy = sasDisks.filter(d=>d.health_ok).length;
  let sataHealthy = sataDisks.filter(d=>d.health_ok).length;
  let dockerStr = (dk.running||0)+' 运行中';
  let statusBar = `
  <div class="statusbar">
    <div class="status-left"><span class="status-dot"></span><span>系统运行正常 · 最近检测 ${D.time||''}</span></div>
    <div class="status-right">
      <div class="status-item"><span class="k">散热风扇</span><span class="v" style="color:${fanOk?'var(--green)':'var(--red)'}">${fanStr}</span></div>
      <div class="status-item"><span class="k">阵列卡</span><span class="v" style="color:${raidOk?'var(--green)':'var(--yellow)'}">${raidStr}</span></div>
      ${sasDisks.length?`<div class="status-item"><span class="k">SAS硬盘</span><span class="v" style="color:${sasHealthy===sasDisks.length?'var(--green)':'var(--orange)'}">${sasHealthy+'/'+sasDisks.length} 健康</span></div>`:''}
      ${sataDisks.length?`<div class="status-item"><span class="k">SATA硬盘</span><span class="v" style="color:${sataHealthy===sataDisks.length?'var(--green)':'var(--orange)'}">${sataHealthy+'/'+sataDisks.length} 健康</span></div>`:''}
      <div class="status-item"><span class="k">Docker</span><span class="v" style="color:var(--blue)">${dockerStr}</span></div>
    </div>
  </div>`;
  // 系统配置
  let cpuPct = s.cpu_threads ? Math.min(Math.round(parseFloat((s.load&&s.load[0])||0)/s.cpu_threads*100),100) : 0;
  let memMods = (s.memory_modules && s.memory_modules.modules) || [];
  let memModRows = memMods.length ? memMods.map(m=>{
    let stBadge = m.installed ? '<span class="badge b-ok">已用</span>' : '<span class="badge b-warn">空</span>';
    return `<tr style="${m.installed?'':'opacity:.45'}">
      <td>${m.locator||'-'}</td>
      <td>${stBadge}</td>
      <td>${m.brand||m.manufacturer||'-'}</td>
      <td>${m.dram_manufacturer?('颗粒: '+m.dram_manufacturer):'-'}</td>
      <td>${m.part||'-'}</td>
      <td>${m.size||'-'}</td>
      <td>${m.speed||'-'}</td>
    </tr>`;
  }).join('') : '<tr><td colspan=7>无 / 未安装 i2c-tools 或 dmidecode</td></tr>';
  let mm = s.memory_modules;
  let memSummary = mm ? (`插槽 ${mm.installed}/${mm.slots}` + (mm.brand_summary && mm.brand_summary!=='未知' ? ` · ${mm.brand_summary}` : '') + (mm.empty>0 ? ` · ${mm.empty} 空槽` : '')) : '';
  let memCard = `
  <div class="card" style="grid-column:1/-1">
    <h3>内存</h3>
    <div class="kv"><span class="k">总量</span><span class="v">${(s.memory&&s.memory.total)||'-'}</span></div>
    <div class="kv"><span class="k">已用</span><span class="v">${(s.memory&&s.memory.used)||'-'}</span></div>
    <div class="kv"><span class="k">占用率</span><span class="v" style="color:${s.memory&&s.memory.percent>70?'var(--orange)':'var(--green)'}">${(s.memory&&s.memory.percent)||0}%</span></div>
    <div class="kv"><span class="k">内存插槽</span><span class="v">${memSummary||'-'}</span></div>
    <div style="margin-top:8px;font-size:12px;color:var(--muted)">插槽含空槽；品牌取自 SPD 直读</div>
    <div style="overflow-x:auto"><table class="table" style="font-size:12px"><thead><tr><th>插槽</th><th>状态</th><th>模组厂</th><th>颗粒厂</th><th>型号</th><th>容量</th><th>频率</th></tr></thead><tbody>${memModRows}</tbody></table></div>
  </div>`;
  let bm = (s.board && s.board.manufacturer) || "";
  let boardOverride = s.board && s.board.override;
  let boardHasDMI = bm && bm.toLowerCase() !== "default string" && bm.toLowerCase() !== "to be filled by o.e.m.";
  let boardEdit = `
    <span class="edit-pencil" title="标注/修改主板型号" onclick="showBoardEdit()">✎</span>`;
  let boardCard = `
  <div class="card">
    <h3>主板 ${boardEdit}</h3>
    ${boardHasDMI || boardOverride ? `
    ${boardOverride ? '<div style="margin-bottom:6px"><span class="badge b-info">手动标注</span></div>' : ''}
    <div class="kv"><span class="k">品牌</span><span class="v">${(s.board&&s.board.manufacturer)||'-'}</span></div>
    <div class="kv"><span class="k">型号</span><span class="v">${(s.board&&s.board.product)||'-'}</span></div>
    <div class="kv"><span class="k">版本</span><span class="v">${(s.board&&s.board.version)||'-'}</span></div>
    <div class="kv"><span class="k">BIOS</span><span class="v">${(s.board&&s.board.bios_vendor)||'-'} ${(s.board&&s.board.bios_version)||''} (${(s.board&&s.board.bios_date)||''})</span></div>
    <div class="kv"><span class="k">芯片组</span><span class="v">${(s.board&&s.board.chipset)||'未知'}</span></div>
    ` : `
    <div class="kv"><span class="k">芯片组</span><span class="v">${(s.board&&s.board.chipset)||'未知'}</span></div>
    <div class="kv"><span class="k">说明</span><span class="v" style="font-size:12px;color:var(--orange)">${(s.board&&s.board.note)||'BIOS 未写入主板信息'}</span></div>
    `}
    <div id="boardEditBox" style="display:none;margin-top:10px">
      <input id="boardModelInput" class="inp" placeholder="如 豆希 WB360" value="${(s.board&&s.board.product)||''}" style="width:100%;margin-bottom:6px">
      <div style="display:flex;gap:8px">
        <button class="btn-mini b-ok" onclick="saveBoardModel()">保存</button>
        <button class="btn-mini" onclick="hideBoardEdit()">取消</button>
        ${boardOverride ? '<button class="btn-mini b-bad" onclick="clearBoardModel()">清除标注</button>' : ''}
      </div>
    </div>
  </div>`;
  let sysInfoCard = `
  <div class="card">
    <h3>系统信息</h3>
    <div class="kv"><span class="k">主机名</span><span class="v">${s.hostname||'-'}</span></div>
    <div class="kv"><span class="k">系统</span><span class="v">${s.os||'-'}</span></div>
    <div class="kv"><span class="k">内核</span><span class="v">${s.kernel||'-'}</span></div>
    <div class="kv"><span class="k">运行时间</span><span class="v">${s.uptime||'-'}</span></div>
  </div>`;
  let netCard = `
  <div class="card">
    <h3>网络</h3>
    ${(s.nics||[]).map(n=>{let sc=n.state==='up'?'var(--green)':'var(--red)';let sp=n.speed?n.speed+' Mbps':'N/A';return `<div class="kv"><span class="k">${n.name}</span><span class="v">${n.ip||'无IP'} · ${sp} · <span style="color:${sc}">${n.state}</span></span></div>`;}).join('')||'<div class="loading">未检测到</div>'}
  </div>`;
  let sysConfig = `
  <div class="detect-cards">
    <div class="card">
      <h3>处理器</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${s.cpu_model||'-'}</span></div>
      <div class="kv"><span class="k">核心/线程</span><span class="v">${s.cpu_cores||0}核 / ${s.cpu_threads||0}线程</span></div>
      <div class="kv"><span class="k">CPU 温度</span><span class="v" style="color:${tempColor(s.cpu_temp,100)}">${s.cpu_temp!=null?s.cpu_temp+'°C':'N/A'}</span></div>
      <div class="kv"><span class="k">负载占用</span><span class="v">${cpuPct}%</span></div>
      ${(s.gpus&&s.gpus.length)?s.gpus.map(g=>`<div class="kv"><span class="k">显卡</span><span class="v">${g}</span></div>`).join(''):''}
    </div>
    ${boardCard}
    ${memCard}
    ${netCard}
    ${sysInfoCard}
  </div>`;
  // 阵列卡 & 磁盘
  let raidCard;
  if(r.mode==='mega'){
    raidCard = `
    <div class="card">
      <h3>RAID 控制器</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
      <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info">MegaRAID (IR)</span></span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">✓ 正常</span></div>
      <div class="kv"><span class="k">芯片温度</span><span class="v" style="color:${tempColor(r.controller_temp,85)}">${r.controller_temp!=null?r.controller_temp+'°C':'N/A'}</span></div>
      <div class="kv"><span class="k">物理盘</span><span class="v">${r.drives?r.drives.length:0} 块</span></div>
      <div class="kv"><span class="k">固件</span><span class="v">${r.fw_package||'-'}</span></div>
    </div>`;
  } else if(r.mode==='hba'){
    raidCard = `
    <div class="card">
      <h3>控制器 (HBA 直通)</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
      <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info">IT 直通</span></span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">✓ 正常工作</span></div>
      <div class="kv"><span class="k">温度</span><span class="v" style="color:var(--muted)">卡无传感器 · 见「硬盘 SMART」</span></div>
    </div>`;
  } else {
    raidCard = `<div class="card"><h3>阵列卡</h3><div class="loading">${r.note||'未检测到阵列卡'}</div></div>`;
  }
  let diskItem = d => {
    let t = (d.type||'').toUpperCase();
    let isSas = t==='SAS';
    let typeBadge = `<span class="badge ${isSas?'b-sas':'b-sata'}">${isSas?'SAS':'SATA'}</span>`;
    return `<div class="disk-mini">
      <span>${typeBadge} ${d.dev} · ${d.brand?d.brand+' ':''}${d.vendor?d.vendor+' ':''}${d.model}${d.feature?` <span class="badge b-info">${d.feature}</span>`:''}</span>
      <span><span style="color:${tempColor(d.temp,d.temp_trip||60)};font-weight:600">${d.temp!=null?d.temp+'°C':'N/A'}</span> · <span class="badge ${d.health_ok?'b-ok':'b-bad'}">${d.health}</span></span>
    </div>`;
  };
  // 复用上方状态栏已声明的 sasDisks/sataDisks（同 filter 逻辑），避免 let 重复声明致 JS 解析崩溃
  sasDisks = disks.filter(d=>(d.type||'').toUpperCase()==='SAS');
  sataDisks = disks.filter(d=>{let t=(d.type||'').toUpperCase(); return t==='SATA'||t==='ATA';});
  let otherDisks = disks.filter(d=>{let t=(d.type||'').toUpperCase(); return t!=='SAS'&&t!=='SATA'&&t!=='ATA';});
  let diskMini = '';
  if(sasDisks.length) diskMini += `<div class="disk-group-title">SAS 硬盘 (${sasDisks.length})</div>` + sasDisks.map(diskItem).join('');
  if(sataDisks.length) diskMini += `<div class="disk-group-title">SATA 硬盘 (${sataDisks.length})</div>` + sataDisks.map(diskItem).join('');
  if(otherDisks.length) diskMini += `<div class="disk-group-title">其他硬盘 (${otherDisks.length})</div>` + otherDisks.map(diskItem).join('');
  if(!disks.length) diskMini = '<div class="loading">未检测到硬盘</div>';
  let raidDisk = `
  <div class="detect-cards">
    ${raidCard}
    <div class="card">
      <h3>磁盘健康 (SMART)</h3>
      ${diskMini}
    </div>
  </div>`;
  // 存储卷、Docker 容器列表均已移至左侧独立标签页，硬件配置检测仅保留概览状态栏
  return `
  <div class="detect-title">硬件配置检测</div>
  <div class="detect-sub">实时监测设备健康状态与硬件详情 · {{ APP_VERSION }}</div>
  ${statusBar}
  <div class="detect-title" style="font-size:16px;margin-bottom:10px">系统配置</div>
  ${sysConfig}
  <div class="detect-title" style="font-size:16px;margin:18px 0 10px">阵列卡 &amp; 磁盘</div>
  ${raidDisk}
  `;
}

function renderDocker(dk){
  if(!dk) dk={};
  let head = `
  <div class="grid-2">
    <div class="card">
      <h3>容器概览</h3>
      <div class="kv"><span class="k">运行中</span><span class="v" style="color:var(--green)">${dk.running||0}</span></div>
      <div class="kv"><span class="k">总数</span><span class="v">${dk.total||0}</span></div>
      <div class="kv"><span class="k">状态</span><span class="v" style="color:${dk.ok?'var(--green)':'var(--red)'}">${dk.ok?'正常':'读取失败'}</span></div>
    </div>
    <div class="card">
      <h3>说明</h3>
      <div style="font-size:13px;color:var(--muted);line-height:1.7">列出所有容器及其运行状态、占用内存与端口映射。内存仅对运行中容器有效；停止的容器显示 N/A。</div>
    </div>
  </div>`;
  let rows = (dk.containers&&dk.containers.length) ? dk.containers.map(c=>{
    let badge = c.running?'b-ok':'b-warn';
    let stateTxt = c.running?'运行':'停止';
    let mem = c.mem||'N/A';
    let ports = c.ports||'-';
    return `<tr>
      <td><span class="badge ${badge}">${stateTxt}</span></td>
      <td><b>${c.name}</b><br><span style="font-size:12px;color:var(--muted)">${c.image}</span></td>
      <td style="white-space:nowrap">${mem}</td>
      <td style="font-size:12px">${ports}</td>
      <td style="font-size:12px;color:var(--muted)">${c.runtime}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="5" class="loading">未检测到 Docker / 无容器</td></tr>';
  let table = `
  <div class="section-title" style="margin-top:14px">容器列表</div>
  <div class="card">
    <table class="table"><thead><tr><th>状态</th><th>名称 / 镜像</th><th>内存占用</th><th>端口映射</th><th>运行时长</th></tr></thead><tbody>${rows}</tbody></table>
  </div>`;
  return head + table;
}

function renderAll(){
  if(!DATA) return;
  if(DATA.error){
    document.querySelectorAll('.panel').forEach(p=>p.innerHTML='<div class="card"><div class="loading" style="color:var(--red)">加载失败: '+DATA.error+'</div></div>');
    return;
  }
  document.getElementById('detect').innerHTML = renderDetect(DATA);
  document.getElementById('raid').innerHTML = renderRaid(DATA.raid, DATA.disks);
  document.getElementById('disks').innerHTML = renderDisks(DATA.disks);
  document.getElementById('system').innerHTML = renderSystem(DATA.system);
  let _fa=document.activeElement;
  let _fanBusy = _fa && (_fa.classList.contains('fan-slider') || _fa.classList.contains('fan-custom-input'));
  if(!_fanBusy){
    document.getElementById('fan').innerHTML = renderFanControl();
    bindFanSliders();
  }
  // 正在拖动滑块/编辑自定义占空比时跳过风扇页整页重建，避免打断操作；转速数值由 fetchFanStatus 每2秒刷新
  document.getElementById('storage').innerHTML = renderStorage(DATA.storage);
  document.getElementById('docker').innerHTML = renderDocker(DATA.docker);
  document.getElementById('lastUpdate').textContent = '更新于 '+DATA.time+' ('+DATA.elapsed+'s)';
  initDiskTemp();
  initSysTemp();
}

async function loadData(){
  document.getElementById('spin').style.display='inline-block';
  try{
    let r = await fetch('/api/all?_='+Date.now());
    DATA = await r.json();
    renderAll();
  }catch(e){
    document.getElementById('lastUpdate').textContent = '加载失败: '+e;
  }
  document.getElementById('spin').style.display='none';
}

var ar = document.getElementById('autoRefresh');
ar.addEventListener('change', function(){
  if(this.checked){ timer=setInterval(loadData, 30000); }
  else if(timer){ clearInterval(timer); timer=null; }
  try{ localStorage.setItem('nasdash_autorefresh', this.checked ? '1':'0'); }catch(e){}
});
try{
  if(localStorage.getItem('nasdash_autorefresh') === '1'){
    ar.checked = true;
    timer = setInterval(loadData, 30000);
  }
}catch(e){}

loadData();

// ---------- 检测新版本 ----------
let updateDismissed = false;
async function checkUpdate(force){
  const banner = document.getElementById('updateBanner');
  if(!force && updateDismissed) return;
  if(force){
    const btn = document.getElementById('checkUpdateBtn');
    if(btn){ btn.disabled = true; btn.textContent = '🔔 检查中…'; }
  }
  try{
    const r = await fetch('/api/version' + (force ? '?force=1' : ''));
    const d = await r.json();
    const link = document.getElementById('updateLink');
    if(d.update_available && d.latest){
      document.getElementById('updateText').innerHTML = '发现新版本 <b>'+d.latest+'</b>（当前 '+d.current+'）';
      link.href = d.url || 'https://github.com/han951meng/nasdash/releases';
      link.style.display = '';
      banner.classList.add('show');
    } else {
      banner.classList.remove('show');
      link.style.display = 'none';
      if(force){
        if(d.error){
          document.getElementById('updateText').textContent = '检查更新失败：' + d.error;
        } else {
          document.getElementById('updateText').textContent = '已是最新版本（'+d.current+'）';
        }
        banner.classList.add('show');
        if(!d.error){ setTimeout(()=>banner.classList.remove('show'), 3500); }
      }
    }
  }catch(e){
    if(force){
      document.getElementById('updateText').textContent = '检查更新出错：' + e;
      banner.classList.add('show');
    }
  }finally{
    if(force){
      const btn = document.getElementById('checkUpdateBtn');
      if(btn){ btn.disabled = false; btn.textContent = '🔔 检查更新'; }
    }
  }
}
function dismissUpdate(){ document.getElementById('updateBanner').classList.remove('show'); updateDismissed = true; }

checkUpdate(false);
</script>
</body>
</html>
"""

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
