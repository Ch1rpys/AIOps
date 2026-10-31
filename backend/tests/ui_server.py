"""只供浏览器自动测试；数据与真实平台分库，所有视图标记 TEST/MOCK。"""

from app.config import ROOT, Settings
from app.main import create_app
from conftest import TestWorld

settings = Settings(
    _env_file=None,
    data_dir=ROOT / "data/test-ui",
    admin_token="TEST-admin-token",
    demo_mysql_password="private",
    verification_interval=0,
)
world = TestWorld()
app = create_app(settings, world.factory)
