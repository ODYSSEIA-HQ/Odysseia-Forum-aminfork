"""图片自动分级适配器。

结果 Classification：
  auto_rating  模型结论：general / r18 / r18g / suspect（拿不准）/ blocked（违禁）
  rating       写进附件的分级；suspect 时按保守值（至少 r18），等人工审核改
  scores       模型分数 {general, r18, r18g}
  needs_review suspect 时为真 → attachment.review_state = pending_review

实现：
- StubClassifier（测试 / 本地）：文件名带 blocked / r18g / r18 / suspect（不分大小写）就判成对应结果，
  否则全年龄（契约测试的 1×1 PNG 判 general、ready）。
- HttpClassifier（生产）：POST SITE_CLASSIFIER_URL，见 classify() 注释里的请求和响应。
- ManualClassifier（生产没配分级服务时）：一律 suspect → r18 + 待审核。
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class Classification:
    auto_rating: str
    rating: str | None
    scores: dict | None = None
    needs_review: bool = False
    blocked: bool = False


@dataclass
class ClassifyInput:
    attachment_id: int
    filename: str
    mime: str
    size: int
    width: int | None = None
    height: int | None = None
    url: str | None = None
    """短期签名读取地址（外部服务自己去拉文件）。"""
    head: bytes = field(default=b"", repr=False)
    """文件开头的一段字节（桩分级器可以看）。"""


class ClassifierPort(Protocol):
    async def classify(self, item: ClassifyInput) -> Classification: ...


def suspect(scores: dict | None = None, rating: str = "r18") -> Classification:
    return Classification(
        auto_rating="suspect", rating=rating, scores=scores, needs_review=True
    )


class StubClassifier:
    """测试桩：按文件名标记分级，其余一律全年龄。"""

    async def classify(self, item: ClassifyInput) -> Classification:
        name = item.filename.lower()
        if "blocked" in name:
            return Classification(auto_rating="blocked", rating=None, blocked=True)
        if "suspect" in name:
            return suspect({"general": 0.5, "r18": 0.45, "r18g": 0.05})
        if "r18g" in name:
            return Classification(
                auto_rating="r18g",
                rating="r18g",
                scores={"general": 0.0, "r18": 0.1, "r18g": 0.9},
            )
        if "r18" in name:
            return Classification(
                auto_rating="r18",
                rating="r18",
                scores={"general": 0.05, "r18": 0.95, "r18g": 0.0},
            )
        return Classification(
            auto_rating="general",
            rating="general",
            scores={"general": 1.0, "r18": 0.0, "r18g": 0.0},
        )


class ManualClassifier:
    """没有分级服务：全部进人工审核，审核前按 R18 处理（全年龄模式看不到）。"""

    async def classify(self, item: ClassifyInput) -> Classification:
        return suspect()


class HttpClassifier:
    """外部分级服务。

    请求：POST {SITE_CLASSIFIER_URL}，Authorization: Bearer {SITE_CLASSIFIER_TOKEN}（配置了才带）
        {"attachmentId": "123", "url": "<短期签名地址>", "mime": "image/png", "filename": "...",
         "width": 512, "height": 768}
    响应：{"scores": {"general": 0.9, "r18": 0.08, "r18g": 0.02}, "blocked": false}
    映射：blocked → 拦截；r18g ≥ 阈值 → r18g；r18 ≥ 阈值 → r18；否则 general；
         最高分低于 SITE_CLASSIFIER_CONFIDENCE → suspect（保守分级 + 待审核）。
    服务出错 → suspect（不会因为分级服务挂了把图放出去）。
    """

    def __init__(self, config):
        self.config = config

    async def classify(self, item: ClassifyInput) -> Classification:
        import httpx

        headers = {}
        if self.config.classifier_token:
            headers["Authorization"] = f"Bearer {self.config.classifier_token}"
        payload = {
            "attachmentId": str(item.attachment_id),
            "url": item.url,
            "mime": item.mime,
            "filename": item.filename,
            "width": item.width,
            "height": item.height,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.classifier_timeout
            ) as client:
                res = await client.post(
                    self.config.classifier_url, json=payload, headers=headers
                )
                res.raise_for_status()
                data = res.json()
        except Exception:
            logger.exception("分级服务调用失败，附件 %s 转人工审核", item.attachment_id)
            return suspect()
        return self.map_result(data)

    def map_result(self, data: dict) -> Classification:
        if not isinstance(data, dict):
            return suspect()
        if data.get("blocked"):
            return Classification(
                auto_rating="blocked",
                rating=None,
                blocked=True,
                scores=data.get("scores"),
            )
        raw = data.get("scores") or {}
        try:
            scores = {k: float(raw.get(k, 0.0)) for k in ("general", "r18", "r18g")}
        except (TypeError, ValueError, AttributeError):
            return suspect()
        if scores["r18g"] >= self.config.classifier_r18g_threshold:
            rating = "r18g"
        elif scores["r18"] >= self.config.classifier_r18_threshold:
            rating = "r18"
        else:
            rating = "general"
        if max(scores.values()) < self.config.classifier_confidence:
            return suspect(scores, "r18g" if rating == "r18g" else "r18")
        return Classification(auto_rating=rating, rating=rating, scores=scores)


_classifier: ClassifierPort | None = None


def get_classifier() -> ClassifierPort:
    global _classifier
    if _classifier is None:
        from site_v1.files_config import get_files_config

        config = get_files_config()
        if config.classifier == "http":
            _classifier = HttpClassifier(config)
        elif config.classifier == "stub":
            _classifier = StubClassifier()
        else:
            _classifier = ManualClassifier()
    return _classifier


def set_classifier(classifier: ClassifierPort | None) -> None:
    global _classifier
    _classifier = classifier


# ---------------- 图片宽高 ----------------


def image_size(head: bytes) -> tuple[int, int] | None:
    """从文件开头解析 PNG / GIF / JPEG / WebP 的宽高；认不出返回 None。"""
    try:
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(
                head[20:24], "big"
            )
        if head[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(head[6:8], "little"), int.from_bytes(
                head[8:10], "little"
            )
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            chunk = head[12:16]
            if chunk == b"VP8X":
                w = int.from_bytes(head[24:27], "little") + 1
                h = int.from_bytes(head[27:30], "little") + 1
                return w, h
            if chunk == b"VP8 ":
                return (
                    int.from_bytes(head[26:28], "little") & 0x3FFF,
                    int.from_bytes(head[28:30], "little") & 0x3FFF,
                )
            if chunk == b"VP8L":
                bits = int.from_bytes(head[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if head[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(head):
                if head[i] != 0xFF:
                    i += 1
                    continue
                marker = head[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(head[i + 2 : i + 4], "big")
                if marker in (
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                ):
                    h = int.from_bytes(head[i + 5 : i + 7], "big")
                    w = int.from_bytes(head[i + 7 : i + 9], "big")
                    return w, h
                i += 2 + length
    except Exception:
        return None
    return None
