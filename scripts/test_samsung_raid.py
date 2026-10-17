#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对本次两个修复的回归/验证用例（不依赖硬件）。

1) 三星 SSD 温度：三星 840 PRO 等用属性 ID 190（Airflow_Temperature_Cel）报温度，
   没有 ID 194。修复后 parse_ata_smart 必须能从 ID 190 解出温度（与 get_disk_temps 口径对齐）。
2) 阵列卡物理盘进磁盘列表：get_raid_card 返回 mode=mega + drives（物理盘 SN 不在块设备列表里），
   _enrich_disk_channels 必须把「OS 里无 /dev 节点」的物理盘补为 raid_only 进磁盘列表。
"""
import app as app


# ---------------- 1. 三星 SSD 温度（ID 190 无 ID 194） ----------------
def test_parse_ata_smart_samsung_840pro_id190_only():
    # 真实三星 840 PRO 风格：只有 ID 190，没有 ID 194（修复前此处 temp=None）
    sample = """\
SMART Attributes Data Structure revision number: 10
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   092   092   000    Old_age   Always       -       12345
190 Airflow_Temperature_Cel 0x0032   071   046   000    Old_age   Always       -       29 (Min/Max 19/30)
"""
    d = app.parse_ata_smart(sample)
    assert d.get("temp") == 29, "三星盘 ID 190 温度未被解析，temp=%r" % d.get("temp")


def test_parse_ata_smart_id194_priority_over_190():
    # 同时存在 194 与 190 时，194 优先（避免重复/冲突）
    sample = """\
SMART Attributes Data Structure revision number: 10
ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
194 Temperature_Celsius     0x0022   071   046   000    Old_age   Always       -       35
190 Airflow_Temperature_Cel 0x0032   071   046   000    Old_age   Always       -       29 (Min/Max 19/30)
"""
    d = app.parse_ata_smart(sample)
    assert d.get("temp") == 35, "194 应优先于 190，temp=%r" % d.get("temp")


# ---------------- 2. 阵列卡物理盘补进磁盘列表（raid_only） ----------------
def _make_pd(slot, sn, model, intf="SAS", temp=30, state="Onln"):
    return {
        "slot": slot, "sn": sn, "model": model, "intf": intf,
        "size": "8.0T", "temp": temp, "state": state,
        "brand": "希捷(Seagate)", "feature": "", "rpm": "7200 rpm",
        "size_note": "", "dg": "0", "pd_health": {},
    }


def test_enrich_raid_only_appends_physical_disks():
    # 块设备列表里只有 RAID5 虚拟盘 sdd（其 SN 是 VD 序列号，不会命中任何物理盘 SN）
    disks = [{
        "dev": "sdd", "type": "ata", "serial": "VD-SERIAL-XYZ",
        "model": "MR9362-8i", "health": "OK", "health_ok": True,
    }]
    raid = {
        "mode": "mega",
        "model": "MR9362-8i",
        "locate_supported": True,
        "drives": [
            _make_pd("252:0", "SN-PD-1", "ST8000NM0001"),
            _make_pd("252:1", "SN-PD-2", "ST8000NM0001"),
            _make_pd("252:2", "SN-PD-3", "ST8000NM0001"),
            _make_pd("252:3", "SN-PD-4", "ST8000NM0001"),
            _make_pd("252:4", "SN-PD-5", "ST8000NM0001"),
            _make_pd("252:5", "SN-PD-6", "ST8000NM0001", state="GHS"),   # 热备
            _make_pd("252:6", "SN-PD-7", "ST8000NM0001", state="UG"),    # ugood
        ],
    }
    out = app._enrich_disk_channels(disks, raid)
    raid_only = [d for d in out if d.get("raid_only")]
    # 7 块物理盘补进列表，且虚拟盘本身不重复
    assert len(raid_only) == 7, "应补出 7 块 raid_only 盘，实际 %d" % len(raid_only)
    assert len(out) == 8, "总数应为 1(VD)+7(PD)=8，实际 %d" % len(out)
    # 每块 raid_only 盘都带唯一 dev_id 与阵列卡通道
    dev_ids = {d.get("dev_id") for d in raid_only}
    assert len(dev_ids) == 7, "raid_only 盘 dev_id 应唯一"
    assert all(d.get("channel_type") == "raid" for d in raid_only)
    assert all(d.get("serial") for d in raid_only), "raid_only 盘应带 SN"
