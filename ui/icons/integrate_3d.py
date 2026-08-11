#!/usr/bin/env python3
"""把生成的 3D 图标缩到 256x256 并覆盖到 nasdash/ui/images/icon-*.png。"""
import os
from PIL import Image

SRC = "/Users/hanyu/Workbuddy/2026-07-01-17-37-29/generated-images"
DST = "/Users/hanyu/WorkBuddy/2026-07-01-17-37-29/nasdash/ui/images"
SIZE = 256

MAP = {
    "icon-detect.png":     "A_3D_isometric_icon_of_a_compu_2026-08-10T19-28-06.png",
    "icon-history.png":    "A_3D_isometric_icon_of_an_upwa_2026-08-10T19-28-41.png",
    "icon-raid.png":       "A_single_3D_isometric_RAID_con_2026-08-11T00-05-06.png",
    "icon-hdd.png":        "A_single_3D_isometric_hard_dis_2026-08-11T00-05-07.png",
    "icon-storage.png":    "A_3D_isometric_database_storag_2026-08-10T19-28-43.png",
    "icon-fan.png":        "A_3D_isometric_cooling_fan__cl_2026-08-10T19-28-42.png",
    "icon-docker.png":     "A_single_3D_isometric_docker_w_2026-08-11T00-04-58.png",
    "icon-automation.png": "A_3D_isometric_gear_cog_settin_2026-08-10T19-28-44.png",
    "icon-manual.png":     "A_3D_isometric_open_book_docum_2026-08-10T19-28-43.png",
    "icon-about.png":      "A_single_3D_isometric_informat_2026-08-11T00-05-07.png",
    "icon-system.png":     "A_single_3D_isometric_CPU_micr_2026-08-10T19-37-11.png",
}

total_in = total_out = 0
for name, src in MAP.items():
    sp = os.path.join(SRC, src)
    dp = os.path.join(DST, name)
    im = Image.open(sp).convert("RGBA")
    im = im.resize((SIZE, SIZE), Image.LANCZOS)
    im.save(dp, "PNG", optimize=True)
    sin = os.path.getsize(sp); sout = os.path.getsize(dp)
    total_in += sin; total_out += sout
    print(f"{name:22s} {sin/1024:7.1f}KB -> {sout/1024:7.1f}KB")

print(f"\n总: {total_in/1024/1024:.2f}MB -> {total_out/1024/1024:.2f}MB")
