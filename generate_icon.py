from PIL import Image, ImageDraw
import os


def create_icon(size, filename, base=1024):
    img = Image.new('RGBA', (base, base), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    blue = (59, 130, 246, 255)   # #3B82F6 ( brighter blue, closer to reference )
    white = (255, 255, 255, 255)
    green = (34, 197, 94, 255)   # #22C55E

    # 圆角方形背景（饱满大圆角，接近参考图风格）
    margin = 32
    draw.rounded_rectangle(
        [(margin, margin), (base - margin, base - margin)],
        radius=240,
        fill=blue
    )

    # 三条白杠（更粗、更圆、间距更均匀）
    bar_w = 540
    bar_h = 135
    bar_x = (base - bar_w) // 2
    bar_radius = 65
    gap = 175
    bar_y1 = (base - (3 * bar_h + 2 * gap)) // 2
    bar_y2 = bar_y1 + bar_h + gap
    bar_y3 = bar_y2 + bar_h + gap

    for y in (bar_y1, bar_y2, bar_y3):
        draw.rounded_rectangle(
            [(bar_x, y), (bar_x + bar_w, y + bar_h)],
            radius=bar_radius,
            fill=white
        )

    # 三个绿色状态灯（更大，偏左内嵌）
    dot_r = 40
    dot_x = bar_x + 78
    for y in (bar_y1, bar_y2, bar_y3):
        cy = y + bar_h // 2
        draw.ellipse(
            [(dot_x - dot_r, cy - dot_r), (dot_x + dot_r, cy + dot_r)],
            fill=green
        )

    resized = img.resize((size, size), Image.LANCZOS)
    resized.save(filename, 'PNG')


if __name__ == '__main__':
    out_dir = os.path.dirname(os.path.abspath(__file__))
    # 关键（参考 hermes-agent fpk 的成功做法）：飞牛桌面/应用中心显示图标时会
    # 缩放，位图分辨率太低就糊。别按官方文档把 "64" 做成真 64×64——那会模糊。
    # hermes 的诀窍：无论文件名叫 64 还是 256，位图内容全部用 256×256 高清。
    # 飞牛实测不校验 ICON 尺寸（hermes 的 ICON.PNG 也是 256×256）。
    HI = 256
    create_icon(HI, os.path.join(out_dir, 'ICON.PNG'))
    create_icon(HI, os.path.join(out_dir, 'ICON_256.PNG'))
    # 应用入口/桌面/应用中心真正显示的图标来自 ui/images/icon-{0}.png
    # （ui/config 引用 "images/icon-{0}.png"，fnOS 把 {0} 替换成 64/256）
    create_icon(HI, os.path.join(out_dir, 'ui', 'images', 'icon-64.png'))
    create_icon(HI, os.path.join(out_dir, 'ui', 'images', 'icon-256.png'))
    print('ICON.PNG: 256x256 (高清位图，对齐 hermes 做法)')
    print('ICON_256.PNG: 256x256')
    print('ui/images/icon-64.png: 256x256 (文件名 64，内容高清)')
    print('ui/images/icon-256.png: 256x256')
