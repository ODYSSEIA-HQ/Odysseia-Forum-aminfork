"""模拟股市的上市公司和板块（照搬 web 仓库 src/mock/stocks/companies.ts）。

只用文字，不用 logo；代码是游戏内的，不是真实交易所代码。行情和传闻都是模拟的。
"""

STOCK_SECTOR_NAMES = {"frontier": "前沿闭源", "open": "开源模型", "cn_app": "国产应用"}
STOCK_SECTORS = [{"id": k, "name": v} for k, v in STOCK_SECTOR_NAMES.items()]

STOCK_COMPANIES = [
    {"symbol": "OAI", "name": "OpenAI", "product": "ChatGPT", "sector": "frontier", "blurb": "ChatGPT 和 GPT 系列模型的开发者。", "ipoPrice": 120},
    {"symbol": "ANT", "name": "Anthropic", "product": "Claude", "sector": "frontier", "blurb": "Claude 系列模型的开发者，主打安全和长文本。", "ipoPrice": 95},
    {"symbol": "GDM", "name": "Google DeepMind", "product": "Gemini", "sector": "frontier", "blurb": "Gemini 系列模型，背靠 Google。", "ipoPrice": 110},
    {"symbol": "XAI", "name": "xAI", "product": "Grok", "sector": "frontier", "blurb": "Grok 系列模型的开发者。", "ipoPrice": 60},
    {"symbol": "MTA", "name": "Meta AI", "product": "Llama", "sector": "open", "blurb": "开源 Llama 系列模型。", "ipoPrice": 70},
    {"symbol": "MIS", "name": "Mistral", "product": "Le Chat", "sector": "open", "blurb": "来自法国的开源模型公司。", "ipoPrice": 35},
    {"symbol": "DSK", "name": "DeepSeek", "product": "DeepSeek", "sector": "open", "blurb": "深度求索，开源模型以性价比出名。", "ipoPrice": 48},
    {"symbol": "QWN", "name": "阿里通义", "product": "通义千问", "sector": "open", "blurb": "阿里的通义千问（Qwen）系列模型。", "ipoPrice": 40},
    {"symbol": "KIM", "name": "月之暗面", "product": "Kimi", "sector": "cn_app", "blurb": "Kimi 智能助手，长文本起家。", "ipoPrice": 28},
    {"symbol": "ZPU", "name": "智谱", "product": "GLM", "sector": "cn_app", "blurb": "GLM 系列模型和智谱清言。", "ipoPrice": 25},
    {"symbol": "DBO", "name": "字节跳动", "product": "豆包", "sector": "cn_app", "blurb": "豆包大模型和豆包 App。", "ipoPrice": 55},
    {"symbol": "MMX", "name": "MiniMax", "product": "海螺", "sector": "cn_app", "blurb": "海螺 AI 和语音、视频生成模型。", "ipoPrice": 22},
]

COMPANY_BY_SYMBOL = {c["symbol"]: c for c in STOCK_COMPANIES}
