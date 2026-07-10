#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 硬件监控面板 (fnOS Hardware Dashboard)
单文件 Flask 应用：阵列卡状态 / 硬盘 SMART / 系统资源 / 存储卷
部署目录: /opt/fnos-dash/
"""
import subprocess, json, re, os, time, socket, platform, shutil, sys
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# 应用根目录（用来存放手动标注等运行时配置）
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOARD_OVERRIDE_FILE = os.path.join(APP_DIR, "board_override.txt")

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
    elif "SAMSUNG" in model_u or model_u.startswith("SV"):
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
        devs = [d for d in os.listdir("/dev") if re.match(r"^sd[a-z]$", d)]
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
                model = parts[12] if len(parts) > 12 else ""
                brand, feature = disk_brand_and_feature(model)
                # 用每张盘的序列号匹配 smartctl 真实转速（storcli 不提供 RPM）
                e, s = parts[0].split(":")
                sn = ""
                try:
                    sn_out = sudo(f"{STORCLI} /c0 /e{e} /s{s} show all", 15)
                    sn_m = re.search(r"SN\s*=\s*(\S+)", sn_out)
                    if sn_m:
                        sn = sn_m.group(1).strip()
                except Exception:
                    pass
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
        b, f = disk_brand_and_feature(disk["model"])
        disk["brand"] = b
        disk["feature"] = f
        disk["rpm"] = rpm_map.get(disk.get("serial", "").upper(), "") if disk.get("serial") else ""
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
    # 风扇控制信息：优先 FanControlServer 配置，其次 sysfs（不依赖任何外部应用）
    fan_info = {}
    # 1) FanControlServer 配置（可选）—— 提供风扇名称/模式，并借 pwm_path 推断可写路径
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
    # 2) sysfs hwmon —— 不需要 FanControlServer，直接读 it87/nct 芯片的 PWM
    import glob as _glob
    for _hp in sorted(_glob.glob("/sys/class/hwmon/hwmon*")):
        _cn = run(f"cat {_hp}/name 2>/dev/null", 2).strip()
        if _cn.startswith(("it87", "nct")):
            for _fi in range(1, 6):
                _fk = f"fan{_fi}"
                _pe = run(f"cat {_hp}/pwm{_fi}_enable 2>/dev/null", 2).strip()
                _pv = run(f"cat {_hp}/pwm{_fi} 2>/dev/null", 2).strip()
                _controllable = bool(_pe)
                if _fk in fan_info:
                    # FanControlServer 已知的风扇：优先用配置里的 pwm_path，兜底用当前 hwmon
                    if not fan_info[_fk].get("hwmon"):
                        fan_info[_fk]["hwmon"] = _hp
                        fan_info[_fk]["idx"] = _fi
                    fan_info[_fk]["controllable"] = fan_info[_fk].get("controllable") or _controllable
                elif _pe:
                    # 仅 sysfs 暴露的风扇，用 sysfs 模式兜底
                    _mm = {"0": "off", "1": "manual", "2": "auto"}
                    fan_info[_fk] = {"name": f"风扇{_fi}", "mode": _mm.get(_pe, ""),
                                     "hwmon": _hp, "idx": _fi, "controllable": _controllable}
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
                                "controllable": fi.get("controllable", False),
                                "hwmon": fi.get("hwmon", ""),
                                "idx": fi.get("idx", 0),
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
    return render_template_string(HTML)

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
    """设置风扇转速：手动 PWM（带安全下限）或恢复自动控温"""
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
    FLOOR = 30  # 最低 30%，避免停转导致过热
    if mode == "auto":
        sudo(f"bash -c 'echo 2 > {hwmon}/pwm{idx}_enable'")
        return jsonify({"ok": True, "mode": "auto"})
    try:
        pct = int(pwm)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid pwm"}), 400
    pct = max(FLOOR, min(100, pct))
    raw = round(pct / 100 * 255)
    sudo(f"bash -c 'echo 1 > {hwmon}/pwm{idx}_enable; echo {raw} > {hwmon}/pwm{idx}'")
    return jsonify({"ok": True, "mode": "manual", "pwm": pct, "raw": raw})


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
  <aside class="sidebar">
    <div class="sidebar-title">监控模块</div>
    <div class="tabs">
      <button class="tab active" onclick="switchTab('detect',this)">🔧 硬件配置检测</button>
      <button class="tab" onclick="switchTab('raid',this)">💾 阵列卡</button>
      <button class="tab" onclick="switchTab('disks',this)">💿 硬盘 SMART</button>
      <button class="tab" onclick="switchTab('system',this)">📊 系统资源</button>
      <button class="tab" onclick="switchTab('storage',this)">🗄️ 存储卷</button>
      <button class="tab" onclick="switchTab('docker',this)">🐳 Docker</button>
    </div>
  </aside>
  <main class="content">
    <div id="detect" class="panel active"><div class="loading">加载中…</div></div>
    <div id="raid" class="panel"><div class="loading">加载中…</div></div>
    <div id="disks" class="panel"><div class="loading">加载中…</div></div>
    <div id="system" class="panel"><div class="loading">加载中…</div></div>
    <div id="storage" class="panel"><div class="loading">加载中…</div></div>
    <div id="docker" class="panel"><div class="loading">加载中…</div></div>
  </main>
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
      <div class="kv"><span class="k">型号</span><span class="v">${d.brand?d.brand+' ':''}${d.vendor?d.vendor+' ':''}${d.model}</span></div>
      <div class="kv"><span class="k">容量 / 类型</span><span class="v">${d.size} · ${isSAS?'SAS':'SATA'} · ${d.rota=='1'?'HDD':'SSD'}${d.feature?' · '+d.feature:''}</span></div>
      <div class="kv"><span class="k">转速</span><span class="v">${d.rpm||(d.rota=='1'||isSAS?'—':'固态(SSD)')}</span></div>
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
      <div class="fan-ctrl-wrap">${renderFanControls(sens.fans)}</div>
    </div>
    <div class="card">
      <h3>电压</h3>
      ${voltsHtml||'<div class="loading">无数据</div>'}
    </div>
  </div>`;
}

function renderFanControls(fans){
  let ctrls = (fans||[]).filter(f=>f.controllable).map(f=>{
    let pct = (f.pwm!=null) ? f.pwm : 50;
    let id = 'fan'+f.idx;
    return `<div class="fan-ctrl">
      <div class="fan-ctrl-head"><span>${f.name}</span><span class="fan-ctrl-val" id="${id}-val">${pct}%</span></div>
      <input type="range" min="30" max="100" value="${pct}" class="fan-slider" data-hwmon="${f.hwmon}" data-idx="${f.idx}" id="${id}-slider">
      <div class="fan-ctrl-actions">
        <button class="btn-mini" onclick="setFanAuto('${f.hwmon}', ${f.idx})">恢复自动</button>
        <span class="fan-ctrl-state" id="${id}-state"></span>
      </div>
    </div>`;
  }).join('');
  if(!ctrls) return `<div class="loading">未检测到可调控风扇（部分 NAS 由系统固件统一控温，本工具不接管）</div>`;
  return `<div class="fan-ctrl-list">${ctrls}</div>`;
}
async function applyFan(hwmon, idx, pwm){
  let id='fan'+idx, st=document.getElementById(id+'-state');
  if(st) st.textContent='设置中…';
  try{
    let r=await fetch('/api/fan/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hwmon:hwmon,idx:idx,pwm:pwm})});
    let j=await r.json();
    if(st) st.textContent = j.ok ? ('已设为 '+pwm+'%') : ('失败：'+(j.error||''));
    if(j.ok){ let v=document.getElementById(id+'-val'); if(v) v.textContent=pwm+'%'; }
  }catch(e){ if(st) st.textContent='请求失败'; }
}
async function setFanAuto(hwmon, idx){
  let id='fan'+idx, st=document.getElementById(id+'-state');
  if(st) st.textContent='恢复中…';
  try{
    let r=await fetch('/api/fan/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hwmon:hwmon,idx:idx,mode:'auto'})});
    let j=await r.json();
    if(st) st.textContent = j.ok ? '已恢复自动控温' : ('失败：'+(j.error||''));
    if(j.ok) setTimeout(loadData, 800);
  }catch(e){ if(st) st.textContent='请求失败'; }
}
function bindFanSliders(){
  document.querySelectorAll('.fan-slider').forEach(sl=>{
    sl.addEventListener('input', ()=>{
      let v=sl.value, id='fan'+sl.dataset.idx, val=document.getElementById(id+'-val');
      if(val) val.textContent=v+'%';
    });
    sl.addEventListener('change', ()=>{
      applyFan(sl.dataset.hwmon, parseInt(sl.dataset.idx), parseInt(sl.value));
    });
  });
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
  <div class="detect-sub">实时监测设备健康状态与硬件详情 · v1.6.0</div>
  ${statusBar}
  <div class="detect-title" style="font-size:16px;margin-bottom:10px">系统配置</div>
  ${sysConfig}
  <div class="detect-title" style="font-size:16px;margin:18px 0 10px">阵列卡 &amp; 磁盘</div>
  ${raidDisk}
  <div class="detect-title" style="font-size:16px;margin:18px 0 10px">风扇控制</div>
  <div class="card">${renderFanControls(s.sensors.fans)}</div>
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
  document.getElementById('storage').innerHTML = renderStorage(DATA.storage);
  document.getElementById('docker').innerHTML = renderDocker(DATA.docker);
  document.getElementById('lastUpdate').textContent = '更新于 '+DATA.time+' ('+DATA.elapsed+'s)';
  bindFanSliders();
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
    _env_port = (os.environ.get("TRIM_SERVICE_PORT") or "").strip()
    port = int(_env_port) if _env_port else 9800
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
