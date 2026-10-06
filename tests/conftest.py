"""测试环境使用隔离数据目录；生产数据库、报告和鉴权配置不参与测试。"""

import os
import tempfile
from pathlib import Path


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="agrisky-tests-"))
os.environ["AGRISKY_DB_PATH"] = str(_TEST_ROOT / "agrisky-test.db")
os.environ["AGRISKY_OUTPUT_ROOT"] = str(_TEST_ROOT / "outputs")
os.environ["AGRISKY_AUTH_ENABLED"] = "false"
os.environ["AGRISKY_SEED_DEMO_DATA"] = "true"
os.environ["AGRISKY_REQUIRE_CLAIM_DOCUMENTS"] = "false"
os.environ["AGRISKY_ALLOW_MOCK_REMOTE_SENSING"] = "true"
