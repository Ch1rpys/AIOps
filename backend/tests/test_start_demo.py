"""双击启动脚本的本机配置：只补空口令，不覆盖已有密钥。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("start_demo", ROOT / "scripts/start_demo.py")
start_demo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(start_demo)


def test_fill_env_keeps_existing_secrets_and_fills_blanks():
    text = "\n".join(
        [
            "# 注释保留",
            "ADMIN_TOKEN=already-set",
            "PLANNER_API_KEY=sk-keep",
            "DEMO_MYSQL_PASSWORD=",
            "HOLMES_MODEL=openai/qwen3.8-max",
            "",
        ]
    )
    updated, filled = start_demo.fill_env(text)
    assert "# 注释保留" in updated
    assert "ADMIN_TOKEN=already-set" in updated
    assert "PLANNER_API_KEY=sk-keep" in updated
    assert "HOLMES_MODEL=openai/qwen3.8-max" in updated
    assert "DEMO_MYSQL_PASSWORD=\n" not in updated
    assert "ADMIN_TOKEN" not in filled
    assert "DEMO_MYSQL_PASSWORD" in filled
    assert "DEMO_MYSQL_ROOT_PASSWORD" in filled
    assert "HOLMES_API_KEY" in filled


def test_choose_node_archive_matches_host_kind():
    listing = "\n".join(
        [
            "abc  node-v22.21.0-darwin-arm64.tar.gz",
            "def  node-v22.21.0-darwin-x64.tar.gz",
            "ghi  node-v22.21.0-win-x64.zip",
            "jkl  node-v22.21.0-win-arm64.zip",
            "mno  node-v22.21.0-linux-arm64.tar.gz",
        ]
    )
    assert start_demo.choose_node_archive(listing, "darwin", "arm64") == (
        "v22.21.0",
        "node-v22.21.0-darwin-arm64.tar.gz",
    )
    assert start_demo.choose_node_archive(listing, "win", "amd64")[1].endswith("win-x64.zip")


def test_missing_model_keys_ignores_local_tokens():
    assert start_demo.missing_model_keys(
        {"ADMIN_TOKEN": "local", "OPENAI_API_KEY": "", "PLANNER_API_KEY": "set"}
    ) == ["OPENAI_API_KEY", "VISION_API_KEY"]
