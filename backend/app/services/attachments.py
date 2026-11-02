"""附件服务：受控存储截图并提取观测。

见 docs/落地执行计划.md §5.2 与 §5.3：
- 第一版只接受真实上传的错误页面与运维截图，不接受把日志画成图再识图；
- 只接受有限格式与大小，走后台受控存储，密钥不落前端；
- **上传时间不能代替截图实际发生时间**：图里没有时间戳就标「时间未知」；
- 历史截图显示异常、当前探针健康时，提示「时间或证据冲突」并补查，
  图片本身**不决定**恢复 PASS。
"""

import base64
import binascii
import hashlib
from pathlib import Path

from app.agents.evidence import attachment_conflict
from app.config import DomainError, Settings
from app.integrations.vision import VisionProvider
from app.schemas.domain import Probe, now

ALLOWED_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def safe_filename(name: str) -> str:
    """只保留可打印字符：文件名会出现在证据与日志里，不能带控制字符伪造行。"""
    cleaned = "".join(char for char in name if char.isprintable()).strip()
    cleaned = cleaned.replace("/", "-").replace("\\", "-")
    return cleaned[:120] or "screenshot"


def sniff_mime(content: bytes) -> str:
    """按魔数判定图片格式，不信任文件名与客户端声明的类型。"""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    raise DomainError("UNSUPPORTED_ATTACHMENT", "只接受 PNG、JPEG 或 WebP 图片", 415)


class AttachmentService:
    def __init__(self, settings: Settings, provider: VisionProvider):
        self.settings = settings
        self.provider = provider

    @property
    def folder(self) -> Path:
        return self.settings.data_dir / "attachments"

    def store(self, filename: str, content_base64: str) -> dict:
        """校验并落盘，返回附件元信息（尚未识图）。"""
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise DomainError("INVALID_ATTACHMENT", "附件不是合法的 base64 内容", 400) from error
        if not content:
            raise DomainError("INVALID_ATTACHMENT", "附件内容为空", 400)
        if len(content) > self.settings.attachment_max_bytes:
            raise DomainError(
                "ATTACHMENT_TOO_LARGE",
                f"附件超过大小上限（{self.settings.attachment_max_bytes} 字节）",
                413,
            )
        mime = sniff_mime(content)
        digest = hashlib.sha256(content).hexdigest()
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{digest}.{ALLOWED_MIME[mime]}"
        if not path.exists():
            path.write_bytes(content)
        return {
            "id": f"att_{digest[:16]}",
            "filename": safe_filename(filename),
            "mime": mime,
            "sha256": digest,
            "size": len(content),
            "source": "user_upload",
            "uploaded_at": now(),
            "storage": str(path.relative_to(self.settings.data_dir)),
            "observed_at": None,
            "observed_at_known": False,
            "extracted_text": "",
            "anomalies": [],
            "uncertain": [],
            "model": self.settings.vision_model or "not_configured",
        }

    def analyze(self, attachment: dict, note: str = "") -> dict:
        """调用视觉模型提取观测，并补齐「时间未知」等显式标注。"""
        path = self.settings.data_dir / attachment["storage"]
        content = path.read_bytes()
        mime = attachment["mime"]
        observation = self.provider.analyze(base64.b64encode(content).decode("ascii"), mime, note)
        updated = {
            **attachment,
            "observed_at": observation.observed_at,
            "observed_at_known": observation.observed_at is not None,
            "extracted_text": observation.extracted_text,
            "anomalies": [item.model_dump(mode="json") for item in observation.anomalies],
            "uncertain": list(observation.uncertain),
            "model": self.settings.vision_model or "service_default",
            "analyzed_at": now(),
        }
        if not updated["observed_at_known"]:
            updated["uncertain"] = [
                *updated["uncertain"],
                "截图内没有可识别的观测时间，时间未知",
            ]
        return updated

    def conflict_note(self, attachment: dict, probes: list[Probe]) -> str | None:
        """截图与当前探针状态是否冲突（判据与证据构造同源）。"""
        return attachment_conflict(attachment, probes)
