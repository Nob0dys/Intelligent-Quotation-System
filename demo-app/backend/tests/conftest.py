import os
import sys
import tempfile
from pathlib import Path


TEST_ROOT = Path(tempfile.mkdtemp(prefix="quote-api-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["QUOTE_DATA_DIR"] = str(TEST_ROOT / "data")
# 系统库的显示名（界面上看到的那个中文名）。测试里换成一个测试专用的名字，
# 免得"生产默认名"一改就撞上测试里自己创建的库——库名唯一是正确行为，
# 不该由生产命名来背这个锅。
os.environ["QUOTE_SYSTEM_DB_NAME"] = "测试系统库"
os.environ["HISTORY_SEED_PATH"] = str(Path(__file__).resolve().parents[2] / "public" / "demo" / "history.json")
os.environ["QUOTE_RUN_INLINE_JOBS"] = "true"
os.environ["DEFAULT_ADMIN_PASSWORD"] = "admin123"
