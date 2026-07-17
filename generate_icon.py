from PIL import Image, ImageDraw
import os


def create_icon(size, filename, base=1024):
    img = Image.new('RGBA', (base, base), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    blue = (37, 99, 235, 255)   # #2563EB
    white = (255, 255, 255, 255)
    green = (34, 197, 94, 255)  # #22C55E

    # 圆角方形背景（饱满圆角tile，贴近系统图标风格；留白适度）
    margin = 32
    draw.rounded_rectangle(
        [(margin, margin), (base - margin, base - margin)],
        radius=200,
        fill=blue
    )

    # 三条白杠（服务器）
    bar_w = 720
    bar_h = 140
    bar_x = (base - bar_w) // 2
    bar_radius = 70
    gap = 60
    bar_y1 = 262
    bar_y2 = bar_y1 + bar_h + gap
    bar_y3 = bar_y2 + bar_h + gap

    for y in (bar_y1, bar_y2, bar_y3):
        draw.rounded_rectangle(
            [(bar_x, y), (bar_x + bar_w, y + bar_h)],
            radius=bar_radius,
            fill=white
        )

    # 三个绿色状态灯
    dot_r = 36
    dot_x = bar_x + 90
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
    # 飞牛规范：包图标 ICON.PNG 必须 64x64，ICON_256.PNG 必须 256x256
    create_icon(64, os.path.join(out_dir, 'ICON.PNG'))
    create_icon(256, os.path.join(out_dir, 'ICON_256.PNG'))
    # 应用入口/桌面/应用中心真正显示的图标来自 ui/images/icon-{0}.png
    # （ui/config 引用 "images/icon-{0}.png"，fnOS 把 {0} 替换成 64/256）
    create_icon(64, os.path.join(out_dir, 'ui', 'images', 'icon-64.png'))
    create_icon(256, os.path.join(out_dir, 'ui', 'images', 'icon-256.png'))
    print('ICON.PNG: 64x64 (fnOS spec, 包图标)')
    print('ICON_256.PNG: 256x256 (fnOS spec, 包图标)')
    print('ui/images/icon-64.png: 64x64 (真正显示用)')
    print('ui/images/icon-256.png: 256x256 (真正显示用)')
