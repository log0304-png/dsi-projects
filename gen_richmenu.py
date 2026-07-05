# -*- coding: utf-8 -*-
from PIL import Image, ImageDraw, ImageFont
import os

W, H = 2500, 843
COLS, ROWS = 4, 2
CW, CH = W // COLS, H // ROWS

BG      = "#0D2137"
CELL_BG = "#112B47"
BORDER  = "#1A5080"
TEXT    = "#FFFFFF"

BUTTONS = [
    ("📖", "使用說明"),
    ("🎙️", "開始會議"),
    ("⏹️", "結束會議"),
    ("💡", "喚醒BOT"),
    ("➕", "新增任務"),
    ("📋", "查行動事項"),
    ("📊", "專案總覽"),
    ("🧾", "上傳發票"),
]

img  = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

try:
    font_path = "C:/Windows/Fonts/msjh.ttc"
    font_big  = ImageFont.truetype(font_path, 90)
    font_icon = ImageFont.truetype("C:/Windows/Fonts/seguiemj.ttf", 100)
except Exception:
    font_big  = ImageFont.load_default()
    font_icon = font_big

for i, (icon, label) in enumerate(BUTTONS):
    col = i % COLS
    row = i // COLS
    x0, y0 = col * CW, row * CH
    x1, y1 = x0 + CW, y0 + CH

    draw.rectangle([x0+8, y0+8, x1-8, y1-8], fill=CELL_BG, outline=BORDER, width=3)

    try:
        bbox = font_icon.getbbox(icon)
        iw, ih = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        iw, ih = 100, 100
    ix = x0 + (CW - iw) // 2
    iy = y0 + CH // 2 - ih - 20
    draw.text((ix, iy), icon, font=font_icon, fill=TEXT)

    try:
        bbox = font_big.getbbox(label)
        lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        lw, lh = 200, 80
    lx = x0 + (CW - lw) // 2
    ly = y0 + CH // 2 + 20
    draw.text((lx, ly), label, font=font_big, fill=TEXT)

out = os.path.join(os.path.dirname(__file__), "richmenu.png")
img.save(out)
print(f"已產生：{out}")