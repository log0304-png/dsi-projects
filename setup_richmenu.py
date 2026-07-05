# -*- coding: utf-8 -*-
"""一次性執行：上傳圖文選單到會議 BOT"""
import requests

LINE_TOKEN = "XNHgKOy4Lhq+vazHtrFsCsx4OxZGWtejKUxarl0+lpoql3nOEHKtVTjmKHFqim6cOg6pCaa4ePKse4/KkWmDFxDONGDYzzzEmF7s9Maxyt75PWot7WpLKCDh7xZlRfuysyK0hE06EfI4z/ZeITYh0QdB04t89/1O/w1cDnyilFU="
IMAGE_PATH = "richmenu.png"

HEADERS = {
    "Authorization": f"Bearer {LINE_TOKEN}",
    "Content-Type": "application/json",
}

RICH_MENU = {
    "size": {"width": 2500, "height": 843},
    "selected": True,
    "name": "會議Bot選單",
    "chatBarText": "快速選單",
    "areas": [
        {"bounds": {"x": 0,    "y": 0,   "width": 625, "height": 421}, "action": {"type": "message", "text": "使用說明"}},
        {"bounds": {"x": 625,  "y": 0,   "width": 625, "height": 421}, "action": {"type": "message", "text": "開始會議"}},
        {"bounds": {"x": 1250, "y": 0,   "width": 625, "height": 421}, "action": {"type": "message", "text": "結束會議"}},
        {"bounds": {"x": 1875, "y": 0,   "width": 625, "height": 421}, "action": {"type": "message", "text": "喚醒BOT"}},
        {"bounds": {"x": 0,    "y": 421, "width": 625, "height": 422}, "action": {"type": "message", "text": "#"}},
        {"bounds": {"x": 625,  "y": 421, "width": 625, "height": 422}, "action": {"type": "message", "text": "查行動事項"}},
        {"bounds": {"x": 1250, "y": 421, "width": 625, "height": 422}, "action": {"type": "uri",     "uri": "https://dsi-projects.onrender.com"}},
        {"bounds": {"x": 1875, "y": 421, "width": 625, "height": 422}, "action": {"type": "message", "text": "上傳發票"}},
    ],
}

print("1. 建立圖文選單...")
r = requests.post(
    "https://api.line.me/v2/bot/richmenu",
    headers=HEADERS,
    json=RICH_MENU,
)
print(f"   狀態：{r.status_code}")
if r.status_code != 200:
    print(f"   失敗：{r.text}")
    exit(1)

rich_menu_id = r.json()["richMenuId"]
print(f"   richMenuId：{rich_menu_id}")

print("2. 上傳圖片...")
with open(IMAGE_PATH, "rb") as f:
    r = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
        headers={"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "image/png"},
        data=f,
    )
print(f"   狀態：{r.status_code}")
if r.status_code != 200:
    print(f"   失敗：{r.text}")
    exit(1)

print("3. 設為預設選單...")
r = requests.post(
    f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
    headers=HEADERS,
)
print(f"   狀態：{r.status_code}")
if r.status_code != 200:
    print(f"   失敗：{r.text}")
    exit(1)

print("\n完成！圖文選單已套用。")