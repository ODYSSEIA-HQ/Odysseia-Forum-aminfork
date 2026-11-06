"""本地假的图片分级服务（按 site_v1/classifier.py HttpClassifier 的接口约定）。

用途：本地联调、集成测试（tests/site_v1/test_site_v1_classifier_http.py 在进程内起它）。
真服务（WD tagger 等）照这个形状实现即可，见 docs/site_v1/README.md「图片分级服务接口」。

单独跑：
  uv run python scripts/fake_classifier.py --port 8835 [--token secret]
  # 后端：SITE_CLASSIFIER=http SITE_CLASSIFIER_URL=http://127.0.0.1:8835/classify
  #       SITE_CLASSIFIER_TOKEN=secret SITE_PUBLIC_BASE=http://127.0.0.1:8831（本地存储时）

行为：先按请求里的 url 把图拉下来（拉不到 502），核对字节数、sha256；然后按文件名里的关键词出结果
（不分大小写，先匹配先用）：
  timeout   睡 SLEEP 秒（默认 5）再回 general，用来测超时
  error     500
  garbage   200 但不是 JSON
  nan       分数里有 NaN
  blocked   {"blocked": true}
  lowconf   分数都不高（最高 0.55），按阈值是「拿不准」
  svc-suspect  服务自己给 rating=suspect
  r18g      r18g 0.92
  r18       r18 0.93
  其余      general 0.98
"""

import argparse
import asyncio
import hashlib
import json

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response


def create_app(*, token: str | None = None, sleep: float = 5.0, base: str | None = None) -> FastAPI:
    """base：图片地址是相对路径时拼的前缀（正常应是绝对地址，不用设）。"""
    app = FastAPI(title="fake classifier")
    app.state.requests = []  # 收到的请求（测试断言用）

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/classify")
    async def classify(request: Request) -> Response:
        if token and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        record = {"body": body, "fetched": None, "error": None}
        app.state.requests.append(record)

        url = body.get("url") or ""
        if url.startswith("/") and base:
            url = base.rstrip("/") + url
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                got = await http.get(url)
            got.raise_for_status()
            data = got.content
        except Exception as exc:  # noqa: BLE001
            record["error"] = f"拉图失败：{exc!r}"
            return JSONResponse({"error": "fetch_failed"}, status_code=502)
        record["fetched"] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "contentType": got.headers.get("content-type"),
        }
        if body.get("size") is not None and body["size"] != len(data):
            record["error"] = "字节数和请求里的 size 不一致"
        if body.get("sha256") and body["sha256"] != record["fetched"]["sha256"]:
            record["error"] = "sha256 不一致"

        name = str(body.get("filename") or "").lower()
        if "timeout" in name:
            await asyncio.sleep(sleep)
            return JSONResponse({"scores": {"general": 0.98, "r18": 0.01, "r18g": 0.01}})
        if "error" in name:
            return JSONResponse({"error": "boom"}, status_code=500)
        if "garbage" in name:
            return PlainTextResponse("<html>not json</html>")
        if "nan" in name:
            return Response(
                '{"scores": {"general": NaN, "r18": 0.1, "r18g": 0.0}}',
                media_type="application/json",
            )
        if "blocked" in name:
            return JSONResponse({"blocked": True, "scores": {"general": 0.0, "r18": 1.0, "r18g": 0.0}})
        if "lowconf" in name:
            return JSONResponse({"scores": {"general": 0.55, "r18": 0.4, "r18g": 0.05}})
        if "svc-suspect" in name:
            return JSONResponse(
                {"rating": "suspect", "scores": {"general": 0.2, "r18": 0.75, "r18g": 0.05}}
            )
        if "r18g" in name:
            return JSONResponse({"scores": {"general": 0.03, "r18": 0.05, "r18g": 0.92}})
        if "r18" in name:
            return JSONResponse({"scores": {"general": 0.05, "r18": 0.93, "r18g": 0.02}})
        return JSONResponse({"scores": {"general": 0.98, "r18": 0.01, "r18g": 0.01}})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8835)
    parser.add_argument("--token", default=None)
    parser.add_argument("--sleep", type=float, default=5.0)
    parser.add_argument("--base", default=None)
    args = parser.parse_args()
    import uvicorn

    app = create_app(token=args.token, sleep=args.sleep, base=args.base)
    print(json.dumps({"listen": f"http://{args.host}:{args.port}/classify"}))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
