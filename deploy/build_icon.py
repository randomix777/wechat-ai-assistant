# -*- coding: utf-8 -*-
"""生成应用图标：候选方向 + 预览拼版 + 多尺寸 .ico。

    .venv\\Scripts\\python.exe deploy\\build_icon.py            # 出候选预览
    .venv\\Scripts\\python.exe deploy\\build_icon.py --final a  # 定稿打包

设计约束（都来自产品本身，不是随手挑的）：
* 底色用界面主题的品牌绿 `ui_theme.BRAND`，深墨 `ui_theme.INK` —— 图标和软件
  是同一套颜色，装到桌面才像同一个东西。
* 形状自己画、不缩位图：Windows 任务栏和资源管理器会用 16/20/24/32 px，
  位图缩下去就糊成一团。这里按 4 倍超采样画完再降采样。
* 圆角方（squircle）外形、四周留 6% 安全边，和 Win11 图标的观感一致。
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SS = 4  # 超采样倍数
SIZE = 1024  # 设计尺寸
ASSETS = Path(__file__).resolve().parent / "assets"
CANDIDATES = ASSETS / "candidates"
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
FONT_BOLD_PATH = Path("C:/Windows/Fonts/msyhbd.ttc")

#: 界面主题里的品牌色，图标沿用
BRAND = "#0e9f6e"
BRAND_DARK = "#0b7d57"
BRAAND_LIGHT = "#19d89b"
INK = "#1a1d21"


# ---------------------------------------------------------------- 基础工具
def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def squircle_mask(size: int, n: float = 4.6, inset: float = 0.06) -> Image.Image:
    """圆角方（超椭圆）蒙版。n 越大越接近方角，4.6 是 Win11/macOS 的观感。"""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    center = (size - 1) / 2
    radius = size / 2 * (1 - inset)
    dist = (np.abs((x - center) / radius) ** n) + (np.abs((y - center) / radius) ** n)
    return Image.fromarray(((dist <= 1).astype(np.uint8) * 255), "L")


def linear_gradient(size: int, start: str, end: str, angle: float = 135.0) -> Image.Image:
    rad = math.radians(angle)
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    t = x * math.cos(rad) + y * math.sin(rad)
    t = (t - t.min()) / max(float(t.max() - t.min()), 1e-6)
    a = np.array(_rgb(start), np.float32)
    b = np.array(_rgb(end), np.float32)
    arr = a[None, None, :] * (1 - t[..., None]) + b[None, None, :] * t[..., None]
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def glow(size: int, cx: float, cy: float, radius: float, color: str, alpha: float) -> Image.Image:
    """柔光层：中间最亮、边缘平滑衰减到 0。"""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.sqrt(((x - cx * size) / (radius * size)) ** 2 + ((y - cy * size) / (radius * size)) ** 2)
    a = (np.clip(1.0 - d, 0, 1) ** 2) * (alpha * 255)
    layer = Image.new("RGBA", (size, size), _rgb(color) + (0,))
    layer.putalpha(Image.fromarray(a.astype(np.uint8), "L"))
    return layer


def star_points(cx: float, cy: float, radius: float, inner: float = 0.30,
                rotation: float = -90.0, points: int = 4) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i in range(points * 2):
        ang = math.radians(rotation + i * (360.0 / (points * 2)))
        r = radius if i % 2 == 0 else radius * inner
        out.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return out


def _u(value: float, size: int) -> float:
    """把 0~1 的相对坐标换成像素。"""
    return value * size


def _rect(box: tuple[float, float, float, float], size: int) -> list[float]:
    return [_u(v, size) for v in box]


def paint(base: Image.Image, mask: Image.Image, fill: Image.Image | tuple,
          alpha: float = 1.0) -> Image.Image:
    """把 fill（图像或纯色）按 mask 画到 base 上。"""
    if isinstance(fill, tuple):
        layer = Image.new("RGBA", base.size, fill + (255,))
    else:
        layer = fill.convert("RGBA")
    if alpha < 1.0:
        a = np.asarray(mask, np.float32) * alpha
        mask = Image.fromarray(a.astype(np.uint8), "L")
    return Image.composite(layer, base, mask)


def shadow(base: Image.Image, mask: Image.Image, size: int, color: str = "#000000",
           alpha: float = 0.45, dy: float = 0.014, blur: float = 0.02) -> Image.Image:
    blurred = mask.filter(ImageFilter.GaussianBlur(_u(blur, size)))
    shifted = Image.new("L", base.size, 0)
    shifted.paste(blurred, (0, int(_u(dy, size))))
    layer = Image.new("RGBA", base.size, _rgb(color) + (0,))
    layer.putalpha(Image.fromarray((np.asarray(shifted, np.float32) * alpha).astype(np.uint8), "L"))
    return Image.alpha_composite(base, layer)


def new_mask(size: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    mask = Image.new("L", (size, size), 0)
    return mask, ImageDraw.Draw(mask)


# ---------------------------------------------------------------- 三个方向
def render_prism(size: int) -> Image.Image:
    """A：棱镜气泡 —— 深色底 + 白色对话气泡 + 光谱星芒（AI 感最强）。"""
    background = linear_gradient(size, "#0A1020", "#1B2A4A", 125.0).convert("RGBA")
    background = Image.alpha_composite(background, glow(size, 0.80, 0.16, 0.55, "#7C5CFF", 0.55))
    background = Image.alpha_composite(background, glow(size, 0.16, 0.86, 0.50, "#12D19A", 0.30))
    background = Image.alpha_composite(background, glow(size, 0.5, 0.02, 0.95, "#FFFFFF", 0.12))

    tile = squircle_mask(size)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = paint(canvas, tile, background)

    # 气泡
    bubble, d = new_mask(size)
    d.rounded_rectangle(_rect((0.170, 0.235, 0.795, 0.645), size),
                        radius=_u(0.115, size), fill=255)
    d.polygon([(_u(0.300, size), _u(0.620, size)),
               (_u(0.250, size), _u(0.795, size)),
               (_u(0.445, size), _u(0.630, size))], fill=255)
    canvas = shadow(canvas, bubble, size, "#000000", 0.50, 0.016, 0.022)
    canvas = paint(canvas, bubble, linear_gradient(size, "#FFFFFF", "#E9F0F8", 120.0))

    # 气泡里的「消息行」，最后一行是品牌的绿（表示 AI 给出来的那条）
    for y, x0, x1, color in (
        (0.325, 0.245, 0.660, "#8FA3BF"),
        (0.405, 0.245, 0.560, "#8FA3BF"),
        (0.487, 0.245, 0.470, BRAND),
    ):
        bar, db = new_mask(size)
        db.rounded_rectangle(_rect((x0, y, x1, y + 0.032), size),
                             radius=_u(0.016, size), fill=255)
        canvas = paint(canvas, bar, _rgb(color))

    # 光谱星芒
    spark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    spark = Image.alpha_composite(spark, glow(size, 0.775, 0.790, 0.30, "#2FE0B4", 0.40))
    star, ds = new_mask(size)
    ds.polygon(star_points(_u(0.775, size), _u(0.790, size), _u(0.170, size)), fill=255)
    spark = paint(spark, star, linear_gradient(size, "#4BEAB4", "#8B5CF6", 60.0))
    canvas = Image.alpha_composite(canvas, spark)

    # 内圈一道极淡的边，让方块在深色壁纸上也有轮廓
    ring, dr = new_mask(size)
    dr.bitmap((0, 0), squircle_mask(size, inset=0.02), fill=255)
    ring = Image.composite(Image.new("L", (size, size), 255), ring,
                           squircle_mask(size, 4.6, 0.06))
    canvas = paint(canvas, ring, _rgb("#FFFFFF"), 0.10)
    return canvas


def render_emerald(size: int) -> Image.Image:
    """B：翡翠对话 —— 品牌绿底 + 双气泡 + 星芒（和软件界面最像一家人）。"""
    background = linear_gradient(size, "#17C08A", "#0A6B4C", 135.0).convert("RGBA")
    background = Image.alpha_composite(background, glow(size, 0.26, 0.16, 0.62, "#FFFFFF", 0.22))
    background = Image.alpha_composite(background, glow(size, 0.88, 0.94, 0.55, "#043B29", 0.40))

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = paint(canvas, squircle_mask(size), background)

    # 后面那个「回声」气泡：半透明白，指向右下
    back, db = new_mask(size)
    db.rounded_rectangle(_rect((0.375, 0.150, 0.855, 0.470), size),
                         radius=_u(0.100, size), fill=255)
    db.polygon([(_u(0.690, size), _u(0.455, size)),
                (_u(0.780, size), _u(0.575, size)),
                (_u(0.605, size), _u(0.462, size))], fill=255)
    canvas = paint(canvas, back, _rgb("#FFFFFF"), 0.22)

    # 前面那个实体气泡，尾巴指向左下
    front, df = new_mask(size)
    df.rounded_rectangle(_rect((0.140, 0.300, 0.710, 0.720), size),
                         radius=_u(0.125, size), fill=255)
    df.polygon([(_u(0.320, size), _u(0.690, size)),
                (_u(0.265, size), _u(0.865, size)),
                (_u(0.465, size), _u(0.700, size))], fill=255)
    canvas = shadow(canvas, front, size, "#043B29", 0.34, 0.016, 0.024)
    canvas = paint(canvas, front, linear_gradient(size, "#FFFFFF", "#EFF7F4", 120.0))

    for y, x0, x1 in ((0.395, 0.215, 0.585), (0.478, 0.215, 0.470)):
        bar, db_ = new_mask(size)
        db_.rounded_rectangle(_rect((x0, y, x1, y + 0.033), size),
                              radius=_u(0.017, size), fill=255)
        canvas = paint(canvas, bar, _rgb(BRAND_DARK))

    spark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    spark = Image.alpha_composite(spark, glow(size, 0.800, 0.760, 0.28, "#FFFFFF", 0.34))
    star, ds = new_mask(size)
    ds.polygon(star_points(_u(0.800, size), _u(0.760, size), _u(0.150, size)), fill=255)
    spark = paint(spark, star, _rgb("#FFFFFF"))
    canvas = Image.alpha_composite(canvas, spark)
    return canvas


def render_ledger(size: int) -> Image.Image:
    """C：墨黑星芒 —— 近黑底 + 翡翠柱状上扬线 + 星芒（最克制、最「工具」）。"""
    background = linear_gradient(size, "#14161A", "#252B35", 130.0).convert("RGBA")
    background = Image.alpha_composite(background, glow(size, 0.50, 0.04, 0.90, "#FFFFFF", 0.10))
    background = Image.alpha_composite(background, glow(size, 0.16, 0.90, 0.60, BRAND_DARK, 0.34))

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = paint(canvas, squircle_mask(size), background)

    ring, dr = new_mask(size)
    dr.bitmap((0, 0), squircle_mask(size, inset=0.035), fill=255)
    ring = Image.composite(Image.new("L", (size, size), 255), ring,
                           squircle_mask(size, 4.6, 0.055))
    canvas = paint(canvas, ring, _rgb("#FFFFFF"), 0.07)

    bars = (0.215, 0.325, 0.435, 0.545)
    heights = (0.160, 0.260, 0.360, 0.480)
    width, baseline = 0.080, 0.775
    gradient = linear_gradient(size, BRAAND_LIGHT, BRAND_DARK, 60.0)
    for x, height in zip(bars, heights):
        bar, db = new_mask(size)
        db.rounded_rectangle(_rect((x, baseline - height, x + width, baseline), size),
                             radius=_u(width / 2, size), fill=255)
        canvas = paint(canvas, bar, gradient)

    # 柱子顶端连一条细线，最后落在星芒上 —— 读消息、出结果
    line = ImageDraw.Draw(canvas)
    points = [(_u(x + width / 2, size), _u(baseline - height - 0.004, size))
              for x, height in zip(bars, heights)]
    points.append((_u(0.760, size), _u(0.245, size)))
    line.line(points, fill=_rgb("#FFFFFF") + (130,), width=int(_u(0.013, size)), joint="curve")

    spark = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    spark = Image.alpha_composite(spark, glow(size, 0.760, 0.245, 0.28, BRAAND_LIGHT, 0.45))
    star, ds = new_mask(size)
    ds.polygon(star_points(_u(0.760, size), _u(0.245, size), _u(0.150, size)), fill=255)
    spark = paint(spark, star, _rgb("#FFFFFF"))
    canvas = Image.alpha_composite(canvas, spark)
    return canvas


DIRECTIONS = {"a": ("棱镜气泡", render_prism),
              "b": ("翡翠对话", render_emerald),
              "c": ("墨黑星芒", render_ledger)}


# ---------------------------------------------------------------- 输出
def render(key: str, size: int = SIZE) -> Image.Image:
    hi = DIRECTIONS[key][1](size * SS)
    return hi.resize((size, size), Image.LANCZOS)


def _font(path: Path, px: int):
    try:
        return ImageFont.truetype(str(path), px)
    except Exception:  # pragma: no cover - 缺字体时退回默认
        return ImageFont.load_default()


def contact_sheet(background: str, out: Path) -> None:
    cell, pad, icon_px = 168, 26, 150
    width = pad * 2 + cell * len(DIRECTIONS)
    height = 330
    sheet = Image.new("RGB", (width, height), _rgb(background))
    draw = ImageDraw.Draw(sheet)
    f_title = _font(FONT_BOLD_PATH, 21)
    f_body = _font(FONT_PATH, 15)
    dark = background.lower() not in ("#ffffff", "#f2f4f7")
    ink = "#f2f4f7" if dark else INK
    for column, (key, (name, _)) in enumerate(DIRECTIONS.items()):
        x = pad + column * cell
        icon = render(key, icon_px)
        sheet.paste(icon, (x, 46), icon)
        draw.text((x + 2, 16), f"{key.upper()} · {name}", font=f_title, fill=ink)
        cursor, row = x, 206
        for size in (48, 32, 24, 16):
            small = icon.resize((size, size), Image.LANCZOS)
            sheet.paste(small, (cursor, row + (48 - size)), small)
            draw.text((cursor, row + 54), f"{size}", font=f_body, fill=ink)
            cursor += size + 8
        draw.text((x + 2, height - 40), "装到桌面就是这张脸", font=f_body, fill=ink)
    sheet.save(out)


def build(finals: list[str]) -> None:
    CANDIDATES.mkdir(parents=True, exist_ok=True)
    for key, (name, _) in DIRECTIONS.items():
        icon = render(key)
        icon.save(CANDIDATES / f"{key}-{name}.png")
    contact_sheet("#ffffff", CANDIDATES / "preview-light.png")
    contact_sheet("#1b1d21", CANDIDATES / "preview-dark.png")
    print(f"候选已写入 {CANDIDATES}")

    for key in finals:
        if key not in DIRECTIONS:
            raise SystemExit(f"未知方向：{key}（可选 {', '.join(DIRECTIONS)}）")
        icon = render(key)
        ASSETS.mkdir(parents=True, exist_ok=True)
        png = ASSETS / "app.png"
        ico = ASSETS / "app.ico"
        icon.save(png)
        sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        icon.save(ico, format="ICO", sizes=sizes)
        print(f"定稿 {key}（{DIRECTIONS[key][0]}）→ {png} / {ico}")


def main() -> None:
    try:  # Windows 控制台可能是 cp1252，中文进度会直接抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--final", default="", help="定稿方向：a / b / c（留空只出候选）")
    args = parser.parse_args()
    finals = [c for c in args.final.strip().lower() if c in "abc"]
    build(finals)


if __name__ == "__main__":
    main()
