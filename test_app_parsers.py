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


def test_enumerate_fans_filters_it87_nct_and_enable(monkeypatch):
    # _enumerate_fans 只认 it87/nct 芯片、且跳过 pwm_enable 为空的风扇口。
    base = "/sys/class/hwmon/hwmon4"

    class FakeGlob:
        def glob(self, pat):
            return [base]

    monkeypatch.setattr(app, "_glob", FakeGlob())

    def fake_run(cmd, *a, **k):
        # 真实命令形如 "cat /sys/class/hwmon/hwmon4/name 2>/dev/null"，不能用 endswith 判断
        if "/name" in cmd:
            return "nct6797\n"
        if "pwm5_enable" in cmd:
            return ""  # 该口不存在（cat 返回空）→ 应跳过
        if "_enable" in cmd:
            return "1\n"
        return ""

    monkeypatch.setattr(app, "run", fake_run)
    fans = app._enumerate_fans(force=True)
    assert (base, 1) in fans
    assert (base, 2) in fans
    assert (base, 3) in fans
    assert (base, 4) in fans
    assert (base, 5) not in fans  # enable=0 跳过


def test_enumerate_fans_skips_non_it87_nct(monkeypatch):
    # 非 it87/nct 芯片（如 coretemp）的风扇不应被纳入控制全集。
    base = "/sys/class/hwmon/hwmon3"

    class FakeGlob:
        def glob(self, pat):
            return [base]

    monkeypatch.setattr(app, "_glob", FakeGlob())
    monkeypatch.setattr(app, "run", lambda cmd, *a, **k: "coretemp\n" if "/name" in cmd else "1\n")
    fans = app._enumerate_fans(force=True)
    assert fans == []
