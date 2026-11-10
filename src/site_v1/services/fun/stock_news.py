"""模拟股市的新闻和传闻（照搬 web 仓库 src/mock/stocks/news.ts）。

- 涨跌幅由 NEWS_RULES 决定并封顶；
- 传闻（rumor）是虚构的随机事件，幅度更小；
- 真实资讯（LLM 每小时拉取并生成事件）还没做：现在按 SITE_FUN_SAMPLE_NEWS（默认开）每个整点按概率
  从 SAMPLE_NEWS 挑一条「示例资讯」，标明是示例，演示整个流程。
"""

import math

from site_v1.services.fun.stock_companies import STOCK_COMPANIES
from site_v1.services.fun.stock_model import (
    mark_volatile,
    next_random,
    push_hype,
    push_trend,
    shift_profit,
    uniform,
)

NEWS_RULES = {
    "stock": {"hype": (1.02, 1.05, 1.09), "profit": (0.005, 0.01, 0.02), "rampMs": 30 * 60_000},
    "sector": {"hype": (1.01, 1.025, 1.045), "rampMs": 45 * 60_000},
    "market": {"trend": (1.005, 1.015, 1.03), "volatileDays": (0, 1, 3), "rampMs": 60 * 60_000},
    "rumor": {"hype": (1.004, 1.015), "rampMs": 20 * 60_000},
}

RUMOR_MEAN_MS = 3 * 3600_000
SAMPLE_NEWS_CHANCE = 0.12


def apply_news(m: dict, e: dict) -> None:
    sign = 1 if e["direction"] == "up" else -1
    i = e["strength"] - 1

    def mul(x):
        return x if sign > 0 else 1 / x

    if e["scope"] == "market":
        push_trend(m, mul(NEWS_RULES["market"]["trend"][i]), NEWS_RULES["market"]["rampMs"])
        days = NEWS_RULES["market"]["volatileDays"][i]
        if days:
            mark_volatile(m, days)
    elif e["scope"] == "sector":
        for s in m["stocks"]:
            if s["sector"] == e["sector"]:
                push_hype(s, mul(NEWS_RULES["sector"]["hype"][i]), NEWS_RULES["sector"]["rampMs"])
    else:
        for s in m["stocks"]:
            if s["symbol"] in e["symbols"]:
                push_hype(s, mul(NEWS_RULES["stock"]["hype"][i]), NEWS_RULES["stock"]["rampMs"])
                shift_profit(s, sign * NEWS_RULES["stock"]["profit"][i])


RUMOR_UP = [
    "{name} 被传正在内测新一代模型",
    "据传 {product} 下周有大版本更新",
    "{name} 被传拿到一笔大额算力订单",
    "社区流传 {product} 新功能截图，讨论热度上升",
    "据传 {name} 正在洽谈新的合作伙伴",
]
RUMOR_DOWN = [
    "据传 {product} 高峰期排队变长",
    "{product} 新版本跑分被传不及预期",
    "据传 {name} 新模型发布推迟",
    "社区反映 {product} 最近回答变短，讨论热度下降",
    "据传 {name} 算力紧张，部分功能限流",
]


def roll_rumor(m: dict) -> dict:
    """随机生成一条传闻并落到模型上（只影响一只股票，强度 1）。"""
    c = STOCK_COMPANIES[math.floor(next_random(m) * len(STOCK_COMPANIES))]
    up = next_random(m) < 0.5
    pool = RUMOR_UP if up else RUMOR_DOWN
    headline = pool[math.floor(next_random(m) * len(pool))].replace("{name}", c["name"]).replace("{product}", c["product"])
    x = uniform(m, NEWS_RULES["rumor"]["hype"])
    s = next(y for y in m["stocks"] if y["symbol"] == c["symbol"])
    push_hype(s, x if up else 1 / x, NEWS_RULES["rumor"]["rampMs"])
    return {
        "scope": "stock",
        "symbols": [c["symbol"]],
        "sector": c["sector"],
        "direction": "up" if up else "down",
        "strength": 1,
        "headline": headline,
        "summary": "市场传闻，未经证实，纯属游戏虚构。",
    }


def rumor_happens(m: dict, ms: int) -> bool:
    return next_random(m) < ms / RUMOR_MEAN_MS


def _n(scope, symbols, sector, direction, strength, headline, summary):
    return {
        "scope": scope, "symbols": symbols, "sector": sector, "direction": direction,
        "strength": strength, "headline": headline, "summary": summary,
    }


SAMPLE_NEWS = [
    _n("market", [], None, "up", 2, "AI 行业融资热度回升", "多家机构上调对大模型赛道的预期，整体情绪转暖。"),
    _n("market", [], None, "down", 2, "算力成本上涨，AI 板块整体降温", "GPU 供应偏紧，市场担心各家推理成本上升。"),
    _n("sector", [], "open", "up", 2, "开源模型在多项榜单追上闭源", "开源社区新一轮模型发布，评测成绩亮眼。"),
    _n("sector", [], "frontier", "down", 1, "闭源模型价格战升级", "多家前沿厂商下调 API 价格，利润预期承压。"),
    _n("sector", [], "cn_app", "up", 1, "国产 AI 应用用户数继续增长", "多款国产助手月活创新高。"),
    _n("stock", ["OAI"], "frontier", "up", 2, "OpenAI 发布新模型，多项评测领先", "新模型在推理和代码评测上刷新成绩。"),
    _n("stock", ["ANT"], "frontier", "up", 2, "Claude 新版本在编程评测中登顶", "开发者社区反馈积极。"),
    _n("stock", ["DSK"], "open", "up", 3, "DeepSeek 开源新模型，性价比再创新低", "以很低的训练成本达到一线水平，引发广泛讨论。"),
    _n("stock", ["GDM"], "frontier", "down", 1, "Gemini 新功能上线后服务短暂不稳定", "部分用户反馈响应变慢，官方称已修复。"),
    _n("stock", ["XAI"], "frontier", "up", 1, "Grok 推出新的多模态能力", "支持更长的视频理解。"),
    _n("stock", ["MTA"], "open", "down", 2, "Llama 新版本评测不及预期", "社区评测成绩低于此前宣传。"),
    _n("stock", ["QWN"], "open", "up", 2, "通义千问发布新一代开源模型", "多个尺寸同时开源，下载量快速上升。"),
    _n("stock", ["KIM"], "cn_app", "up", 1, "Kimi 上线新的长文本功能", "支持一次读完更长的资料。"),
    _n("stock", ["DBO"], "cn_app", "down", 1, "豆包高峰期出现排队", "用户量增长太快，官方称正在扩容。"),
    _n("stock", ["MMX"], "cn_app", "up", 2, "MiniMax 视频生成模型更新", "画面稳定性明显提升。"),
    _n("stock", ["ZPU"], "cn_app", "up", 1, "智谱发布新的智能体产品", "主打办公自动化场景。"),
    _n("stock", ["MIS"], "open", "down", 1, "Mistral 新模型发布推迟", "官方称需要更多时间打磨。"),
]


def pick_sample_news(m: dict) -> dict:
    return SAMPLE_NEWS[math.floor(next_random(m) * len(SAMPLE_NEWS))]
