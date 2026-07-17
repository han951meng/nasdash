#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nasdash 纯函数回归测试（不依赖硬件，可在本地 venv 跑）。

覆盖历史上真实踩过的两个解析 bug：
  - 金士顿 SSD 被误判为三星（v1.7.8 修复）：型号列丢厂商前缀
  - NVMe 通电时长被千分位逗号截断（v1.7.5 修复）："7,442" 读到 7
以及阵列卡芯片温度多格式解析、双磁臂识别。
运行：./test.sh  （自动建 .venv 装 flask+pytest 并跑）
"""
import app as app
import json


# ---------------- 品牌识别（含历史误判回归） ----------------
def test_brand_kingston_full_prefix():
    # 完整型号（含厂商前缀）必须识别为金士顿 —— v1.7.8 修复核心
    brand, feat = app.disk_brand_and_feature("KINGSTON SV300S37A/120G")
    assert brand == "金士顿(Kingston)"
    assert feat == ""


def test_brand_kingston_table_only_no_false_samsung():
    # 历史 bug：型号列只给 "SV300S37A/120G"（无 KINGSTON 前缀）时，
    # 旧代码 startswith("SV") 把它误判三星。修复后无前缀=无品牌，不再误判。
    brand, feat = app.disk_brand_and_feature("SV300S37A/120G")
    assert brand == ""
    assert "三星" not in brand


def test_brand_seagate_dual_actuator():
    brand, feat = app.disk_brand_and_feature("ST14000NM0001")
    assert brand == "希捷(Seagate)"
    assert feat == "双磁臂(双执行器)"


def test_brand_wd_with_space_in_model():
    # "WD PC SN730" 型号首词为 WD，应识别西部数据（不把 model 首词当 vendor 前缀重复）
    brand, feat = app.disk_brand_and_feature("WD PC SN730")
    assert brand == "西部数据(WD)"


def test_brand_samsung_intel_crucial():
    assert app.disk_brand_and_feature("SAMSUNG MZVLB1T0")[0] == "三星(Samsung)"
    assert app.disk_brand_and_feature("INTEL SSDPEKKF512G")[0] == "英特尔(Intel)"
    assert app.disk_brand_and_feature("CT500MX500SSD1")[0] == "英睿达(Crucial)"


def test_brand_unknown_empty():
    assert app.disk_brand_and_feature("") == ("", "")
    assert app.disk_brand_and_feature("SOME-RANDOM-MODEL")[0] == ""


# ---------------- 阵列卡品牌型号解析（v1.7.8 修复逻辑） ----------------
def test_resolve_brand_model_kingston_fix():
    # 表格型号丢前缀 + Inquiry 含前缀 → 必须回退用完整型号，否则误判三星
    assert app._resolve_brand_model("SV300S37A/120G", "KINGSTON SV300S37A/120G") == "KINGSTON SV300S37A/120G"


def test_resolve_brand_model_table_has_vendor_passthrough():
    # 表格型号本身就有厂商前缀 → 保持原样
    assert app._resolve_brand_model("ST14000NM0001", "ST14000NM0001") == "ST14000NM0001"


def test_resolve_brand_model_no_inquiry_keeps_table():
    assert app._resolve_brand_model("SV300S37A/120G", "-") == "SV300S37A/120G"
    assert app._resolve_brand_model("SV300S37A/120G", "") == "SV300S37A/120G"


def test_resolve_brand_model_empty_table_uses_inquiry():
    assert app._resolve_brand_model("-", "KINGSTON SV300S37A/120G") == "KINGSTON SV300S37A/120G"


# ---------------- 阵列卡芯片温度解析（多格式兼容） ----------------
def test_parse_roc_temp_formats():
    assert app._parse_roc_temp("ROC temperature = 56") == 56
    assert app._parse_roc_temp("Controller Temperature = 56") == 56
    assert app._parse_roc_temp("ROC temperature(Degree Celsius) 65") == 65


def test_parse_roc_temp_invalid():
    assert app._parse_roc_temp("") is None
    assert app._parse_roc_temp("no temperature here") is None
    assert app._parse_roc_temp("Temp = abc") is None


# ---------------- NVMe SMART 解析（通电时长逗号修复） ----------------
_NVME_SAMPLE = """
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Temperature:                       41 Celsius
Available Spare:                    100%
Percentage Used:                    3%
Power On Hours:                     7,442
Data Units Read:                    12,345,678 [6.32 TB]
Data Units Written:                 9,876,543 [5.05 TB]
"""


def test_parse_nvme_power_on_hours_comma():
    # v1.7.5 修复：千分位逗号必须去掉，否则 "7,442" 被截断成 7
    d = app.parse_nvme_smart(_NVME_SAMPLE)
    assert d["health"] == "PASSED"
    assert d["power_on_hours"] == 7442
    assert d["temp"] == 41
    assert d["percentage_used"] == 3
    assert d["available_spare"] == 100


def test_parse_nvme_temp_kelvin():
    text = "SMART overall-health self-assessment test result: PASSED\nTemperature:                       320 Kelvin\nPower On Hours:                     1000\n"
    d = app.parse_nvme_smart(text)
    assert d["temp"] == 320 - 273  # 47


def test_parse_nvme_missing_fields_none():
    d = app.parse_nvme_smart("SMART overall-health self-assessment test result: PASSED\n")
    assert d["power_on_hours"] is None
    assert d["temp"] is None
    assert d["health"] == "PASSED"


# ---------------- 风扇温控选择（Bug A 修复回归） ----------------
def _fake_fans():
    base = "/sys/class/hwmon/hwmon4"
    return [(base, 1), (base, 2), (base, 3)]


def test_select_sys_temp_all_claims_every_fan():
    # Bug A 核心：controlled_fans="all" 时，sys_temp 必须接管全集里的每一把风扇，
    # 而不是只接管「用户手动调过的」(FAN_TARGETS)。
    all_fans = _fake_fans()
    sys_cfg = {"enabled": True, "controlled_fans": "all"}
    disk_cfg = {"enabled": False, "disks": []}
    sys_claimed, disk_claimed = app._select_temp_fans(all_fans, sys_cfg, disk_cfg)
    assert sys_claimed == set(all_fans)
    assert disk_claimed == set()


def test_select_disk_temp_all_claims_every_fan():
    all_fans = _fake_fans()
    sys_cfg = {"enabled": False, "controlled_fans": "all"}
    disk_cfg = {"enabled": True, "disks": ["/dev/sda"], "controlled_fans": "all"}
    sys_claimed, disk_claimed = app._select_temp_fans(all_fans, sys_cfg, disk_cfg)
    assert sys_claimed == set()
    assert disk_claimed == set(all_fans)


def test_select_subset_only_claims_listed():
    all_fans = _fake_fans()
    sys_cfg = {"enabled": True, "controlled_fans": [["/sys/class/hwmon/hwmon4", 1]]}
    disk_cfg = {"enabled": False, "disks": []}
    sys_claimed, _ = app._select_temp_fans(all_fans, sys_cfg, disk_cfg)
    assert sys_claimed == {("/sys/class/hwmon/hwmon4", 1)}


def test_select_sys_priority_over_disk():
    # sys_temp 与 disk_temp 都设 "all" 时，sys 先占全部，disk 不得重复控（互不干扰）。
    all_fans = _fake_fans()
    sys_cfg = {"enabled": True, "controlled_fans": "all"}
    disk_cfg = {"enabled": True, "disks": ["/dev/sda"], "controlled_fans": "all"}
    sys_claimed, disk_claimed = app._select_temp_fans(all_fans, sys_cfg, disk_cfg)
    assert sys_claimed == set(all_fans)
    assert disk_claimed == set()


def test_select_disk_only_when_sys_disabled():
    # sys 关、disk 开 all：disk 接管全集。
    all_fans = _fake_fans()
    sys_cfg = {"enabled": False, "controlled_fans": "all"}
    disk_cfg = {"enabled": True, "disks": ["/dev/sda"], "controlled_fans": "all"}
    sys_claimed, disk_claimed = app._select_temp_fans(all_fans, sys_cfg, disk_cfg)
    assert sys_claimed == set()
    assert disk_claimed == set(all_fans)


# ---------------- 风扇枚举（自动检测，换硬件不失效） ----------------
def _fan_enum_harness(monkeypatch, hwmap):
    """构造 _enumerate_fans 的假环境。
    hwmap: {hwmon_path: {"enable":[idx...], "pwm":[idx...], "fan":[idx...]}}
    - _glob.glob 返回 pwm*_enable 路径列表 / 所有 hwmon 目录
    - os.path.exists 按 pwm<N> / fan<N>_input 佐证
    关键：新实现不再按芯片名(it87/nct)过滤，也不限 fan1-5，而是枚举所有 hwmon 的 pwmN_enable。
    """
    import re as _re

    class FakeGlob:
        def glob(self, pat):
            if pat.endswith("pwm*_enable"):
                hp = pat[: -len("pwm*_enable")].rstrip("/")
                return [f"{hp}/pwm{i}_enable" for i in hwmap.get(hp, {}).get("enable", [])]
            if pat.endswith("hwmon*"):
                return list(hwmap.keys())
            return []

    monkeypatch.setattr(app, "_glob", FakeGlob())

    def fake_exists(path):
        m = _re.search(r"/(hwmon\d+)/(pwm|fan)(\d+)(_input)?$", path)
        if not m:
            return False
        for hp_key, spec in hwmap.items():
            if path.startswith(hp_key + "/"):
                kind, idx = m.group(2), int(m.group(3))
                if kind == "pwm":
                    return idx in spec.get("pwm", [])
                return idx in spec.get("fan", [])
        return False

    monkeypatch.setattr(app.os.path, "exists", fake_exists)


def test_enumerate_fans_by_pwm_enable(monkeypatch):
    # 枚举依据是「存在 pwmN_enable 且佐证 pwm/fan_input」，不再依赖芯片名
    hp = "/sys/class/hwmon/hwmon4"
    _fan_enum_harness(monkeypatch, {hp: {"enable": [1, 2, 3, 4], "pwm": [1, 2, 3, 4], "fan": [1, 2, 3, 4]}})
    fans = app._enumerate_fans(force=True)
    assert (hp, 1) in fans and (hp, 2) in fans and (hp, 3) in fans and (hp, 4) in fans
    assert (hp, 5) not in fans  # 无 enable 文件 → 跳过


def test_enumerate_fans_includes_non_it87_nct_chip(monkeypatch):
    # 换非 it87/nct 主板（Fintek f71882fg / 华硕 / AMD）→ 仍应自动枚举风扇
    fintek = "/sys/class/hwmon/hwmon3"
    it87 = "/sys/class/hwmon/hwmon4"
    _fan_enum_harness(monkeypatch, {
        fintek: {"enable": [1, 2], "pwm": [1, 2], "fan": [1, 2]},
        it87:   {"enable": [1], "pwm": [1], "fan": [1]},
    })
    fans = app._enumerate_fans(force=True)
    assert (fintek, 1) in fans and (fintek, 2) in fans
    assert (it87, 1) in fans


def test_enumerate_fans_multi_channel_hub(monkeypatch):
    # 集线器/分线器占用 fan6 通道 → 不漏（旧实现只到 fan5）
    hp = "/sys/class/hwmon/hwmon4"
    _fan_enum_harness(monkeypatch, {hp: {"enable": [1, 2, 3, 4, 5, 6], "pwm": [1, 2, 3, 4, 5, 6], "fan": [1, 2, 3, 4, 5, 6]}})
    fans = app._enumerate_fans(force=True)
    assert all((hp, i) in fans for i in range(1, 7))


def test_enumerate_fans_excludes_rgb_pwm(monkeypatch):
    # 存在 pwm3_enable 但无对应 pwm3 / fan3_input（如 RGB 灯效 pwm）→ 排除
    hp = "/sys/class/hwmon/hwmon4"
    _fan_enum_harness(monkeypatch, {hp: {"enable": [1, 3], "pwm": [1], "fan": [1]}})
    fans = app._enumerate_fans(force=True)
    assert (hp, 1) in fans
    assert (hp, 3) not in fans


# ---------------- 存储控制器检测（厂商白名单放宽） ----------------
def test_detect_storage_controllers_includes_non_whitelisted_vendor(monkeypatch):
    # Areca 等不在旧厂商白名单，但应自动识别为 HBA（换任意品牌卡都不漏）
    out = (
        "01:00.0 RAID bus controller: Areca Technology Corp. ARC-1882 SAS/SATA RAID Controller\n"
        "02:00.0 Non-Volatile memory controller: Samsung Electronics Co Ltd NVMe SSD Controller\n"
    )
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: out if "lspci" in cmd else "")
    cs = app.detect_storage_controllers()
    assert any("Areca" in c["model"] for c in cs)
    assert any(c["is_hba"] for c in cs)
    # 仍正确识别 MegaRAID
    mega_out = "03:00.0 RAID bus controller: Broadcom / LSI MegaRAID SAS 9271-8i\n"
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: mega_out if "lspci" in cmd else "")
    cs2 = app.detect_storage_controllers()
    assert cs2 and cs2[0]["is_megaraid"]


# ---------------- 磁盘枚举（多位盘名 / 分区 / 控制器过滤） ----------------
def test_get_disks_includes_multi_letter_sata_names(monkeypatch):
    # 验证 sdaa / sdab 等多位盘名不被漏掉（原 ls /dev/sd? 只匹配单字母）
    sd_out = "sda\nsdaa\nsdab\nsdb\n"
    nvme_out = "nvme0\nnvme0n1\nnvme0n1p1\n"

    def fake_run(cmd, *a, **k):
        if "ls /dev/sd" in cmd:
            return sd_out
        if "ls /dev/nvme" in cmd:
            return nvme_out
        return ""

    monkeypatch.setattr(app, "run", fake_run)
    monkeypatch.setattr(app, "sudo", lambda *a, **k: "")
    disks = app.get_disks()
    devs = [d["dev"] for d in disks]
    assert "sdaa" in devs and "sdab" in devs
    assert "nvme0n1" in devs
    assert "nvme0" not in devs       # NVMe 控制器排除
    assert "nvme0n1p1" not in devs   # NVMe 分区排除


# ---------------- sys_temp 温度源（CPU 封装 / AMD Tdie 优先） ----------------
def test_fan_read_sys_temp_cpu_prefers_package(monkeypatch):
    sens = json.dumps({
        "coretemp-isa-0000": {
            "Package id 0": {"temp1_input": 55.0},
            "Core 0": {"temp2_input": 50.0},
        },
        "it8620-isa-0290": {"temp1_input": 40.0},
    })
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: sens if "sensors -j" in cmd else "")
    assert app._fan_read_sys_temp("cpu") == 55.0


def test_fan_read_sys_temp_cpu_amd_tdie(monkeypatch):
    # AMD 主板无 coretemp，应优先 Tdie（而非回落所有传感器最大混入 it86 主板温度）
    sens = json.dumps({
        "k10temp-pci-00c3": {
            "Tdie": {"temp1_input": 62.0},
            "Tctl": {"temp2_input": 62.0},
        },
        "it8620-isa-0290": {"temp1_input": 35.0},
    })
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: sens if "sensors -j" in cmd else "")
    assert app._fan_read_sys_temp("cpu") == 62.0


def test_fan_read_sys_temp_mb_excludes_coretemp(monkeypatch):
    sens = json.dumps({
        "coretemp-isa-0000": {"Package id 0": {"temp1_input": 70.0}},
        "it8620-isa-0290": {"temp1_input": 38.0, "temp2_input": 41.0},
    })
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: sens if "sensors -j" in cmd else "")
    assert app._fan_read_sys_temp("mb") == 41.0  # 主板温度取 it86 最高，不含 coretemp
