"""流水线路径配置。可通过环境变量覆盖默认值。"""
from pathlib import Path

from .runtime import resolve_env_path

NEW_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = NEW_DIR.parent

SCAN_FILE = resolve_env_path("FB_SCAN_FILE", NEW_DIR / "facebook-ad-keywords-scan.txt", NEW_DIR)
LEARNED_FILE = resolve_env_path("FB_LEARNED_FILE", NEW_DIR / "facebook-ad-keywords-learned.txt", NEW_DIR)
RESULTS_DIR = resolve_env_path("FB_RESULTS_DIR", SKILLS_DIR / "fb-ad-library-results", SKILLS_DIR)
EXCEL_FILE = resolve_env_path("FB_EXCEL_FILE", NEW_DIR / "facebook-ad-keywords-master.xlsx", NEW_DIR)
INTEL_FILE   = RESULTS_DIR / "brand_intelligence.json"
RABBIT_QUEUE = RESULTS_DIR / "scan_queue_rabbit_hole.txt"
LOG_DIR      = NEW_DIR / "logs"

RESULTS_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
