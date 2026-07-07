#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 硬件监控面板 (fnOS Hardware Dashboard)
单文件 Flask 应用：阵列卡状态 / 硬盘 SMART / 系统资源 / 存储卷
部署目录: /opt/fnos-dash/
"""
import subprocess, json, re, os, time, socket, platform
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# 命令全路径（admin 的 PATH 不含 /usr/sbin）
STORCLI = "/usr/local/bin/storcli"
SMARTCTL = "/usr/sbin/smartctl"
SENSORS = "/usr/bin/sensors"

# ---------- 基础执行 ----------
def run(cmd, timeout=30):
    """执行 shell 命令，返回 stdout 字符串"""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception as e:
        return ""

def sudo(cmd, timeout=30):
    return run("sudo -n " + cmd, timeout)

# ===================== 采集：阵列卡 =====================
def detect_storage_controllers():
    """用 lspci 检测存储控制器，区分 MegaRAID(IR) 与 HBA(IT) 直通卡"""
    out = run("lspci -nn 2>/dev/null", 10)
    controllers = []
    for line in out.splitlines():
        if not re.search(r"(LSI|Avago|Broadcom|Microchip|Adaptec|Marvell|Intel|ASMedia)", line, re.I):
            continue
        if not re.search(r"(RAID|SAS|SCSI|HBA)", line, re.I):
            continue
        m = re.search(r":\s*(.+)$", line)
        model = m.group(1).strip() if m else line.strip()
        is_megaraid = bool(re.search(r"MegaRAID", line, re.I))
        # 含 SAS/HBA 但不含 MegaRAID → 视为 HBA 直通卡（IT 模式）
        is_hba = bool(re.search(r"SAS|HBA", line, re.I)) and not is_megaraid
        controllers.append({"model": model, "is_megaraid": is_megaraid, "is_hba": is_hba})
    return controllers

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
        # 阵列卡芯片温度 (ROC Temperature)
        # 匹配多种 storcli 版本/卡型的输出格式
        temp_m = re.search(r"(?:Controller\s+Temperature|ROC\s+temperature[^=]*)\s*=\s*(\d+)", out, re.I)
        if not temp_m:
            # 备选：从 /c0 show all 输出中解析（需用 show all 而非 show）
            all_out = sudo(f"{STORCLI} /c0 show all", 15)
            temp_m = re.search(r"ROC\s+temperature[^=]*\s*=\s*(\d+)", all_out, re.I)
        if temp_m:
            data["controller_temp"] = int(temp_m.group(1))
        else:
            data["controller_temp"] = None
        # 物理盘列表（用 split 解析表格行，更健壮）
        # 格式: 252:0 21 JBOD - 6.366 TB SAS HDD N N 4 KB ST14000NM0001 U -
        drives = []
        seen_slots = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 12 and re.match(r"^\d+:\d+$", parts[0]):
                if parts[0] in seen_slots:
                    continue
                seen_slots.add(parts[0])
                drives.append({
                    "slot": parts[0], "did": parts[1], "state": parts[2],
                    "dg": parts[3], "size": parts[4] + " " + parts[5],
                    "intf": parts[6], "media": parts[7],
                    "model": parts[12] if len(parts) > 12 else "",
                    "sp": parts[13] if len(parts) > 13 else "",
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
        data["note"] = ("HBA 直通卡（IT 模式）：磁盘由系统内核直接管理，不经阵列卡固件。"
                        "每张盘的温度与 SMART 信息请见「硬盘 SMART」标签页。")
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
            # raw_value 取第一个数字（温度等可能是 "41 (Min/Max -1/56)"）
            num_m = re.match(r"\s*(\d+)", raw_str)
            raw_num = int(num_m.group(1)) if num_m else 0
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

def get_disks():
    """采集所有块设备 + SMART（以 ls /dev/sd? 拿盘名，smartctl 拿详情，不依赖 lsblk 字段对齐）"""
    disks = []
    out = run("ls /dev/sd? 2>/dev/null", 5)
    devnames = sorted(set(l.strip().split('/')[-1] for l in out.split()
                          if l.strip() and re.match(r"^sd[a-z]+$", l.strip().split('/')[-1])))
    # lsblk 补充容量/rota/tran
    lsblk = run("lsblk -dn -b -o NAME,SIZE,ROTA,TRAN 2>/dev/null", 5)
    linfo = {}
    for line in lsblk.strip().splitlines()[1:]:
        p = line.split()
        if len(p) >= 2:
            linfo[p[0]] = {"size_b": p[1], "rota": p[2] if len(p) > 2 else "?",
                           "tran": p[3] if len(p) > 3 else ""}
    for name in devnames:
        dev = f"/dev/{name}"
        info = linfo.get(name, {})
        size_b = info.get("size_b", "0")
        try:
            gb = int(size_b) / 1e9
            size_str = f"{gb/1000:.1f}T" if gb >= 1000 else f"{gb:.0f}G"
        except:
            size_str = "?"
        smart_out = sudo(f"{SMARTCTL} -a {dev}", 20)
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
                # SAS 盘从 smartctl 拿容量（lsblk 对阵列卡后的盘可能返回 0）
                m = re.search(r"User Capacity:\s*([\d,]+)\s*bytes", smart_out)
                if m:
                    cap = int(m.group(1).replace(",", ""))
                    gb = cap / 1e9
                    disk["size"] = f"{gb/1000:.1f}T" if gb >= 1000 else f"{gb:.0f}G"
            elif "overall-health" in smart_out:
                disk["type"] = "ata"
                disk.update(parse_ata_smart(smart_out))
                m = re.search(r"Device Model:\s*(.+)", smart_out)
                if not m:
                    m = re.search(r"Model Family:\s*(.+)", smart_out)
                disk["model"] = m.group(1).strip() if m else ""
                m = re.search(r"Serial Number:\s*(\S+)", smart_out)
                disk["serial"] = m.group(1) if m else ""
                disk["vendor"] = disk["model"].split()[0] if disk["model"] else ""
        disk["health_ok"] = disk["health"].upper() in ("OK", "PASSED")
        disks.append(disk)
    return disks

# ===================== 采集：系统资源 =====================
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
    # 风扇控制信息：优先 FanControlServer 配置，其次 sysfs（不依赖任何外部应用）
    fan_info = {}
    # 1) FanControlServer 配置（可选）—— 提供风扇名称和曲线/手动/关闭模式
    fc_raw = run("cat /vol2/@appconf/FanControlServer/config.json 2>/dev/null", 3)
    if fc_raw:
        try:
            fc = json.loads(fc_raw)
            for fan in fc.get("fans", []):
                idx = fan.get("pwm_index")
                if idx:
                    fan_info[f"fan{idx}"] = {
                        "name": fan.get("name", f"风扇{idx}"),
                        "mode": fan.get("mode", ""),
                    }
        except (json.JSONDecodeError, ValueError):
            pass
    # 2) sysfs hwmon —— 不需要 FanControlServer，直接读 it87/nct 芯片的 PWM
    import glob as _glob
    for _hp in sorted(_glob.glob("/sys/class/hwmon/hwmon*")):
        _cn = run(f"cat {_hp}/name 2>/dev/null", 2).strip()
        if _cn.startswith(("it87", "nct")):
            for _fi in range(1, 6):
                _fk = f"fan{_fi}"
                _pe = run(f"cat {_hp}/pwm{_fi}_enable 2>/dev/null", 2).strip()
                _pv = run(f"cat {_hp}/pwm{_fi} 2>/dev/null", 2).strip()
                # FanControlServer 没覆盖的风扇，用 sysfs 模式兜底
                if _fk not in fan_info and _pe:
                    _mm = {"0": "off", "1": "manual", "2": "auto"}
                    fan_info[_fk] = {"name": f"风扇{_fi}", "mode": _mm.get(_pe, "")}
                # PWM 占空比（0-255 → 百分比），不管装没装 FanControlServer 都读
                if _pv and _fk in fan_info:
                    try:
                        fan_info[_fk]["pwm"] = round(int(_pv) / 255 * 100)
                    except ValueError:
                        pass
            break
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
                            display_name = fi.get("name", default_name)
                            mode = fi.get("mode", "")
                            pwm = fi.get("pwm")
                            d["sensors"]["fans"].append({
                                "name": display_name,
                                "rpm": int(fv),
                                "stopped": fv < 1,
                                "mode": mode,
                                "pwm": pwm,
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

# ===================== 路由 =====================
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/all")
def api_all():
    t0 = time.time()
    try:
        result = {
            "raid": get_raid_card(),
            "disks": get_disks(),
            "system": get_system(),
            "storage": get_storage(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 2),
        }
    except Exception as e:
        result = {"error": str(e), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    return jsonify(result)

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
  .container{max-width:1600px;margin:0 auto;padding:20px}
  .tabs{display:flex;gap:6px;margin-bottom:18px;background:var(--card);padding:6px;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .tab{flex:1;padding:10px 16px;text-align:center;cursor:pointer;border-radius:8px;font-size:14px;font-weight:500;color:var(--muted);transition:all .2s;border:none;background:none}
  .tab:hover{background:#f3f4f6}
  .tab.active{background:var(--blue);color:#fff}
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
  @media(max-width:700px){.grid-2,.grid-auto{grid-template-columns:1fr}.header{flex-direction:column;gap:10px}}
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
  </div>
</div>

<div class="container">
  <div class="tabs">
    <button class="tab active" onclick="switchTab('raid',this)">💾 阵列卡</button>
    <button class="tab" onclick="switchTab('disks',this)">💿 硬盘 SMART</button>
    <button class="tab" onclick="switchTab('system',this)">📊 系统资源</button>
    <button class="tab" onclick="switchTab('storage',this)">🗄️ 存储卷</button>
  </div>

  <div id="raid" class="panel active"><div class="loading">加载中…</div></div>
  <div id="disks" class="panel"><div class="loading">加载中…</div></div>
  <div id="system" class="panel"><div class="loading">加载中…</div></div>
  <div id="storage" class="panel"><div class="loading">加载中…</div></div>
</div>

<script>
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
      let typ = d.type === 'sas' ? 'SAS' : (d.rota === '1' ? 'SATA HDD' : 'SATA SSD');
      return `<tr><td>${d.dev}</td><td>${mdl}</td><td>${typ}</td><td style="color:${tcol};font-weight:600">${tstr}</td></tr>`;
    }).join('') : '<tr><td colspan=4>未检测到磁盘（请用 smartctl 确认盘已被系统识别）</td></tr>';
    return `
    <div class="cards">
      <div class="card">
        <h3>控制器 (HBA 直通)</h3>
        <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
        <div class="kv"><span class="k">模式</span><span class="v"><span class="badge b-info">IT 直通</span></span></div>
        <div class="kv"><span class="k">状态</span><span class="v" style="color:var(--green)">✓ 正常工作</span></div>
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
  let drives = r.drives.map(d=>`<tr><td>${d.slot}</td><td>${d.model}</td><td>${d.intf}</td><td>${d.size}</td><td><span class="badge ${d.state==='JBOD'||d.state==='Onln'?'b-ok':'b-warn'}">${d.state}</span></td><td>${d.sp==='U'?'运转':'停止'}</td></tr>`).join('');
  return `
  <div class="cards">
    <div class="card">
      <h3>阵列卡信息</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${r.model}</span></div>
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
  </div>
  <div class="section-title">物理盘列表</div>
  <div class="card">
    <table class="table"><thead><tr><th>槽位</th><th>型号</th><th>接口</th><th>容量</th><th>状态</th><th>转速</th></tr></thead><tbody>${drives||'<tr><td colspan=6>无</td></tr>'}</tbody></table>
  </div>`;
}

function renderDisks(disks){
  if(!disks||!disks.length) return '<div class="card"><div class="loading">未检测到硬盘</div></div>';
  return '<div class="cards">'+disks.map(d=>{
    let isSAS = d.type==='sas';
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
      <div class="kv"><span class="k">型号</span><span class="v">${d.vendor} ${d.model}</span></div>
      <div class="kv"><span class="k">容量 / 类型</span><span class="v">${d.size} · ${isSAS?'SAS':'SATA'} · ${d.rota=='1'?'HDD':'SSD'}</span></div>
      <div class="kv"><span class="k">序列号</span><span class="v" style="font-size:12px">${d.serial}</span></div>
      <div style="margin-top:8px"><span style="font-size:13px;color:var(--muted)">温度 ${tempStr}</span><div class="temp-bar"><div class="temp-fill" style="width:${tempPct}%;background:${tempColor(d.temp,trip)}"></div></div></div>
      ${detail}
    </div>`;
  }).join('')+'</div>';
}

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
    let pwmStr = (f.pwm!=null) ? ` · PWM ${f.pwm}%` : '';
    return `<div class="kv"><span class="k">${f.name}${modeStr}</span><span class="v" style="color:${color}">${rpmStr}${pwmStr}</span></div>`;
  }).join('');
  let voltsHtml = (sens.voltages||[]).map(v=>`<div class="kv"><span class="k">${v.name}</span><span class="v">${v.value} V</span></div>`).join('');
  let loadColor = (l)=>parseFloat(l)>s.cpu_threads?'var(--red)':parseFloat(l)>s.cpu_threads*0.7?'var(--orange)':'var(--green)';
  return `
  <div class="grid-2">
    <div class="card">
      <h3>CPU</h3>
      <div class="kv"><span class="k">型号</span><span class="v">${s.cpu_model}</span></div>
      <div class="kv"><span class="k">核心/线程</span><span class="v">${s.cpu_cores}核 / ${s.cpu_threads}线程</span></div>
      <div class="kv"><span class="k">最大频率</span><span class="v">${s.cpu_freq} MHz</span></div>
      <div class="kv"><span class="k">CPU 温度</span><span class="v" style="color:${tempColor(s.cpu_temp,100)}">${s.cpu_temp!=null?s.cpu_temp+'°C':'N/A'}</span></div>
      <div class="kv"><span class="k">运行时长</span><span class="v">${s.uptime}</span></div>
    </div>
    <div class="card">
      <h3>显卡</h3>
      ${(s.gpus||[]).map(g=>`<div class="kv"><span class="k">GPU</span><span class="v">${g}</span></div>`).join('')||'<div class="loading">未检测到</div>'}
    </div>
  </div>
  <div class="grid-2" style="margin-top:14px">
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
    <div class="card">
      <h3>系统信息</h3>
      <div class="kv"><span class="k">主机名</span><span class="v">${s.hostname}</span></div>
      <div class="kv"><span class="k">系统</span><span class="v">${s.os}</span></div>
      <div class="kv"><span class="k">内核</span><span class="v">${s.kernel}</span></div>
    </div>
    <div class="card">
      <h3>网卡</h3>
      ${(s.nics||[]).map(n=>{let sc=n.state==='up'?'var(--green)':'var(--red)';let sp=n.speed?n.speed+' Mbps':'N/A';return `<div class="kv"><span class="k">${n.name}</span><span class="v">${n.ip||'无IP'} · ${sp} · <span style="color:${sc}">${n.state}</span></span></div>`;}).join('')||'<div class="loading">未检测到</div>'}
    </div>
  </div>`;
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

function renderAll(){
  if(!DATA) return;
  if(DATA.error){
    document.querySelectorAll('.panel').forEach(p=>p.innerHTML='<div class="card"><div class="loading" style="color:var(--red)">加载失败: '+DATA.error+'</div></div>');
    return;
  }
  document.getElementById('raid').innerHTML = renderRaid(DATA.raid, DATA.disks);
  document.getElementById('disks').innerHTML = renderDisks(DATA.disks);
  document.getElementById('system').innerHTML = renderSystem(DATA.system);
  document.getElementById('storage').innerHTML = renderStorage(DATA.storage);
  document.getElementById('lastUpdate').textContent = '更新于 '+DATA.time+' ('+DATA.elapsed+'s)';
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
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("TRIM_SERVICE_PORT", "9800"))
    app.run(host="0.0.0.0", port=port, debug=False)
