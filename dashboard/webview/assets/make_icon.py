"""WorkBuddy 图标 · 纯线条版生成器

原 logo 是「渐变圆角方块 + 白色动物头剪影」。这里只保留轮廓线条：
  - 外圈圆角方形
  - 动物头轮廓（两只耳朵 + 头部）
  - 两只眼睛（旋转的圆角短条）

输出：
  icon.svg        —— 界面用，stroke=currentColor，随亮/暗主题自动反色
  icon.ico        —— exe 用，含 16/24/32/48/64/128/256 多尺寸
  icon_*.png      —— 中间产物，便于人工核对

用 Pillow 直接描线（4 倍超采样后缩回，得到抗锯齿），
所以不依赖 cairosvg / 浏览器。
"""

import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4          # 超采样倍数
STROKE = 11.0   # 与 SVG 保持一致（256 画布下的线宽）

W = 256


# ---------------------------------------------------------------- 几何

def _cubics_to_points(start, segments, steps=18):
    """把一串三次贝塞尔展平成折线点列。"""
    pts = [start]
    cur = start
    for p1, p2, p3 in segments:
        for i in range(1, steps + 1):
            t = i / steps
            u = 1 - t
            x = (u ** 3 * cur[0] + 3 * u * u * t * p1[0]
                 + 3 * u * t * t * p2[0] + t ** 3 * p3[0])
            y = (u ** 3 * cur[1] + 3 * u * u * t * p1[1]
                 + 3 * u * t * t * p2[1] + t ** 3 * p3[1])
            pts.append((x, y))
        cur = p3
    return pts


# 头部轮廓关键点（按原图 Square150x150Logo 等比映射到 256 画布后手工标定）：
#   左耳尖 (85,60) → 耳间凹口 (138,92) → 右耳尖 (181,44)
#   → 右侧 (223,140) → 右下 (198,214) → 底部 (112,226) → 左侧 (44,160)
HEAD_START = (84, 62)
HEAD_SEGMENTS = [
    ((102, 70), (120, 82), (138, 92)),      # 左耳 -> 凹口
    ((154, 76), (168, 56), (181, 44)),      # 凹口 -> 右耳尖
    ((202, 74), (219, 110), (223, 140)),    # 右耳 -> 右侧
    ((221, 178), (210, 202), (198, 214)),   # 右侧 -> 右下
    ((176, 228), (140, 232), (112, 226)),   # 右下 -> 底部
    ((80, 216), (52, 194), (44, 160)),      # 底部 -> 左侧
    ((42, 122), (62, 80), (84, 62)),        # 左侧 -> 左耳尖（闭合）
]

# 眼睛：两根旋转约 34° 的圆角短条，位于头部右下区域
EYES = (
    (170, 150, 32, 17, 8.5, 34),
    (128, 182, 32, 17, 8.5, 34),
)


def head_points():
    return _cubics_to_points(HEAD_START, HEAD_SEGMENTS)


def rounded_rect_points(x, y, w, h, r, steps=8):
    """圆角矩形轮廓点列（用于描边）。"""
    pts = []
    corners = [
        (x + w - r, y + r, -math.pi / 2, 0),
        (x + w - r, y + h - r, 0, math.pi / 2),
        (x + r, y + h - r, math.pi / 2, math.pi),
        (x + r, y + r, math.pi, 3 * math.pi / 2),
    ]
    for cx, cy, a0, a1 in corners:
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * i / steps
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def rotated(cx, cy, w, h, r, deg):
    pts = rounded_rect_points(cx - w / 2, cy - h / 2, w, h, r)
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    return [(cx + (px - cx) * ca - (py - cy) * sa,
             cy + (px - cx) * sa + (py - cy) * ca) for px, py in pts]


# ---------------------------------------------------------------- 绘制

def draw(size, color):
    """在透明底上画线条图标。"""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    k = s / W                      # 画布坐标 → 像素坐标
    lw = max(1, int(round(STROKE * k)))

    def stroke(points, closed=True):
        pts = [(x * k, y * k) for x, y in points]
        dr.line(pts, fill=color, width=lw, joint="curve")
        if closed:
            dr.line([pts[-1], pts[0]], fill=color, width=lw)
        # 圆头端点，避免折角处出现缺口
        r = lw / 2
        for px, py in (pts if closed else pts[1:-1]):
            dr.ellipse([px - r, py - r, px + r, py + r], fill=color)

    # 外圈圆角方块
    stroke(rounded_rect_points(27, 27, 202, 202, 53))
    # 动物头轮廓
    stroke(head_points())
    # 两只眼睛
    for cx, cy, ew, eh, er, deg in EYES:
        stroke(rotated(cx, cy, ew, eh, er, deg))

    return img.resize((size, size), Image.LANCZOS)


def build_ico():
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [draw(s, (0, 0, 0, 255)) for s in sizes]
    path = os.path.join(HERE, "icon.ico")
    images[-1].save(path, format="ICO",
                    sizes=[(s, s) for s in sizes])
    return path


SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" fill="none">
  <!-- stroke 用 currentColor：界面把它放进 color: var(--text) 即可随主题反色 -->
  <g stroke="currentColor" stroke-width="11" stroke-linecap="round"
     stroke-linejoin="round">
    <rect x="27" y="27" width="202" height="202" rx="53"/>
    <path d="M84 62 C102 70 120 82 138 92
             C154 76 168 56 181 44
             C202 74 219 110 223 140
             C221 178 210 202 198 214
             C176 228 140 232 112 226
             C80 216 52 194 44 160
             C42 122 62 80 84 62 Z"/>
    <rect x="154" y="141.5" width="32" height="17" rx="8.5"
          transform="rotate(34 170 150)"/>
    <rect x="112" y="173.5" width="32" height="17" rx="8.5"
          transform="rotate(34 128 182)"/>
  </g>
</svg>
'''


def main():
    with open(os.path.join(HERE, "icon.svg"), "w", encoding="utf-8") as f:
        f.write(SVG)

    # 预览图：浅色底深色线 / 深色底浅色线，模拟自动反色
    strips = []
    for bg, fg, tag in (((0xF3, 0xF3, 0xF3, 255), (0x1C, 0x1C, 0x1C, 255), "light"),
                        ((0x20, 0x20, 0x20, 255), (0xF1, 0xF1, 0xF1, 255), "dark")):
        sizes = (16, 24, 32, 48, 64, 128)
        gap = 12
        strip = Image.new("RGBA", (sum(sizes) + gap * (len(sizes) + 1), 128 + gap * 2), bg)
        x = gap
        for s in sizes:
            ico_img = draw(s, fg)          # fg 已是 RGBA
            strip.paste(ico_img, (x, gap + (128 - s) // 2), ico_img)
            x += s + gap
        strips.append(strip)

    sheet = Image.new("RGBA", (max(s.width for s in strips),
                               sum(s.height for s in strips)), (255, 255, 255, 255))
    y = 0
    for s in strips:
        sheet.paste(s, (0, y), s)
        y += s.height
    preview = os.path.join(HERE, "icon_preview.png")
    sheet.save(preview)

    ico = build_ico()
    print("icon.svg      ->", os.path.join(HERE, "icon.svg"))
    print("icon_preview  ->", preview)
    print("icon.ico      ->", ico, "(%d bytes)" % os.path.getsize(ico))


if __name__ == "__main__":
    main()