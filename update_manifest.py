#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""精确更新 manifest：version / desc(单行) / changelog 三处，不动 checksum（app.tgz 未变）。"""
import re

MANIFEST = "manifest"

# ---- 新的应用介绍（desc，必须单行，无真实换行）----
NEW_DESC = (
    "<p><b>飞牛OS 硬件监控面板</b> —— 可视化查看 NAS 硬件状态，免去 SSH 敲命令。"
    "</p><p><b>功能模块</b></p><ul>"
    "<li><b>硬件配置检测</b>：单页仪表盘，含顶部状态栏实时概览 + 系统配置（CPU / 显卡 / 主板 / 内存条品牌型号 / 运行时间 / 网络）+ 阵列卡与磁盘概览</li>"
    "<li><b>阵列卡</b>：自动识别卡类型，显示型号 / 模式 / 状态 / 芯片温度与物理盘列表（品牌识别准确，金士顿/希捷等不再误判）</li>"
    "<li><b>硬盘 SMART</b>：每块盘的健康、温度、转速、品牌、容量与型号</li>"
    "<li><b>系统资源</b>：负载 / 内存 / 温度 / 风扇转速 / 电压</li>"
    "<li><b>风扇控制</b>：预设档位一键调速，实时显示 RPM 与占空比；每个风扇可网页自定义名称与供电电压标注；支持两套独立温控——「硬盘温控」按指定硬盘温度调速、「主板/CPU 温控」按 CPU 封装或主板温度调速，各自可设开转/全速/恢复自动温度 + 占空比 + 受控风扇，互不干扰，均带滞回「恢复自动」与真正满转</li>"
    "<li><b>主板型号标注</b>：DMI 为空时支持网页手动标注主板型号并回退识别芯片组</li>"
    "<li><b>内存品牌</b>：SPD 直读（decode-dimms），区分模组厂与颗粒厂</li>"
    "<li><b>存储卷</b>：mdadm 软阵列</li>"
    "<li><b>Docker</b>：容器运行状态、占用内存、端口映射、运行时长</li>"
    "</ul><p><b>阵列卡类型支持</b></p><ul>"
    "<li><b>MegaRAID 卡</b>（如 9271-8i）：型号 / 固件 / CacheVault / 物理盘列表 / 芯片温度</li>"
    "<li><b>HBA 直通卡</b>（如 LSI 9300）：卡型号 + 芯片温度；此类卡 /c0 show 不含温度属正常现象</li>"
    "<li><b>纯 SATA 主板</b>：给出提示，无需额外工具</li>"
    "</ul><p><b>v1.7.8 更新亮点</b></p><ul>"
    "<li>应用图标全面升级为 256×256 高清，修复飞牛桌面与应用中心图标模糊问题（参考 fnos-hermes-agent 的正确做法，无论文件名是否带 64，位图内容统一为 256×256 高清）。</li>"
    "<li>阵列卡物理盘品牌识别修复：storcli 表格将厂商与型号分列，原代码只取型号列导致金士顿(Kingston) SSD 误判为三星；现改用含厂商前缀的完整型号识别。</li>"
    "<li>storcli 二进制随包内置分发：安装时由 install_callback 自动落地到 /opt/MegaRAID/storcli 并建软链，无需再从 Broadcom 官网手动下载；采用 007.2705 新版，兼容 fnOS 内核 6.18（旧版 1.21.06 在新内核下会段错误）。</li>"
    "</ul><p><b>v1.7.7 更新亮点</b></p><ul>"
    "<li>风扇温控曲线拆分为「两套」对称逻辑：原有「硬盘温控」按指定硬盘温度调速；新增「主板/CPU 温控」按 CPU 封装或主板温度调速，各自独立配置，互不干扰，均带滞回「恢复自动」与真正满转。</li>"
    "</ul><p><b>v1.7.6 更新亮点</b></p><ul>"
    "<li>硬盘温控新增「恢复自动温度」滞回控制：盘温降到该温度以下时受控风扇交还自动控速，中间滞回区避免临界抖动；全速档即真正满转；可指定只控部分风扇。</li>"
    "</ul><p><b>依赖说明</b></p><p>安装时自动安装：smartmontools、lm-sensors、mdadm、Flask、dmidecode、i2c-tools（提供 decode-dimms 直读内存 SPD 品牌）。"
    "<br><b>storcli 现已随包内置</b>：MegaRAID 卡与 HBA 直通卡用户无需再从 Broadcom 官网手动下载，安装即自动落地到 /opt/MegaRAID/storcli。</p>"
)
assert "\n" not in NEW_DESC, "desc must be single line!"

# ---- 新的 changelog 开头（1.7.8）----
NEW_CHANGELOG_HEAD = (
    "1.7.8: 应用图标全面升级为 256×256 高清，修复飞牛桌面与应用中心图标模糊（参考 fnos-hermes-agent 做法，位图内容统一 256×256）；"
    "阵列卡物理盘品牌识别修复(金士顿SSD不再误判三星)；storcli 二进制随包内置(安装自动落地无需手动下载，兼容fnOS内核6.18)。 "
)

with open(MANIFEST, encoding="utf-8") as f:
    s = f.read()

# version
s = re.sub(r"(?m)^version\s*=\s*1\.7\.7\b", "version               = 1.7.8", s)

# desc (整行替换)
s = re.sub(r"(?m)^desc\s*=.*$", "desc                  = " + NEW_DESC, s)

# changelog (在 1.7.7: 前插入 1.7.8:)
s = re.sub(
    r"(?m)^changelog\s*=\s*1\.7\.7:",
    "changelog             = " + NEW_CHANGELOG_HEAD + "1.7.7:",
    s,
)

with open(MANIFEST, "w", encoding="utf-8") as f:
    f.write(s)

print("manifest updated: version/desc/changelog -> 1.7.8")
