"""全局配置与路径（已并入 wxMoments 合并项目）。"""
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent
SRC_DIR = BASE_DIR / "src"
WEB_DIR = SRC_DIR / "web"
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_DIR = BASE_DIR / "config"
SESSION_FILE = CONFIG_DIR / "session.json"

for _d in (OUTPUT_DIR, CONFIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


PROXY_PORT = 9527

HOST = "127.0.0.1"
PORT = 8756
