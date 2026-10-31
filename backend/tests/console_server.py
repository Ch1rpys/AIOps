"""浏览器回归专用：三场景外部边界使用显式 TEST/MOCK，独立临时数据库。"""

from tempfile import mkdtemp

from app.config import ROOT, Settings
from app.main import create_app
from test_scenarios import ScenarioWorld

test_root = ROOT / "data/test-ui"
test_root.mkdir(parents=True, exist_ok=True)
settings = Settings(
    _env_file=None,
    data_dir=mkdtemp(prefix="console-test-", dir=test_root),
    admin_token="TEST-admin-token",
    demo_mysql_password="TEST-private",
    verification_interval=0,
    fault_confirmation_interval=0,
)
world = ScenarioWorld()
app = create_app(settings, world.factory)
