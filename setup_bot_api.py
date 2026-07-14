from typing import Any

import requests

from config import TELEGRAM_TOKEN

TOKEN = TELEGRAM_TOKEN
if not TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN trong tệp .env")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"


def call_api(method: str, data: dict[str, Any]) -> dict[str, Any] | str:
    try:
        response = requests.post(f"{BASE_URL}/{method}", json=data, timeout=30)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        # Avoid returning the request URL because it contains the Telegram token.
        return f"Yêu cầu Telegram API thất bại: {type(exc).__name__}"


description = (
    "🎧 Trợ lý âm thanh riêng của Minh Hiếu Producer.\n\n"
    "Gửi liên kết Splice hoặc một trang nhạc để tự động tìm mẫu âm thanh, "
    "kiểm tra chất lượng, chuẩn hóa WAV, phân loại và lưu vào thư viện."
)
print("Desc:", call_api("setMyDescription", {"description": description}))

about = "Trợ lý thu thập, chuẩn hóa và phân loại mẫu âm thanh cho Minh Hiếu Producer."
print("About:", call_api("setMyShortDescription", {"short_description": about}))

commands = [
    {"command": "start", "description": "Mở hướng dẫn sử dụng"},
    {"command": "stats", "description": "Xem thống kê thư viện âm thanh"},
    {"command": "path", "description": "Kiểm tra thư mục đầu ra"},
    {"command": "organize", "description": "Quét một thư mục trên máy"},
]
print("Cmds:", call_api("setMyCommands", {"commands": commands}))
