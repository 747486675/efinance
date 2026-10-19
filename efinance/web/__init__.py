"""Optional Python 3.12 web service. Never imported by the base package."""
import base64
import binascii
import hmac
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..exceptions import (EfinanceError, ParameterError, RateLimitError,
                          SymbolNotFoundError, TimeoutError)
from ..export import csv_chunks
from ..query import normalize_query, query_one, table_payload
from ..settings import get_config

STATIC = Path(__file__).parent / "static"


def create_app(require_auth=False):
    username = os.environ.get("EFINANCE_WEB_USERNAME", "")
    password = os.environ.get("EFINANCE_WEB_PASSWORD", "")
    if (require_auth or username or password) and not (username and password):
        raise ParameterError("Set both EFINANCE_WEB_USERNAME and EFINANCE_WEB_PASSWORD for non-loopback serving")
    auth = bool(username and password)

    @asynccontextmanager
    async def lifespan(app):
        app.state.executor = ThreadPoolExecutor(max_workers=get_config()["max_concurrency"])
        # Bound submitted work as well as active threads: do not build an unbounded queue.
        app.state.capacity = asyncio.Semaphore(get_config()["max_concurrency"])
        try:
            yield
        finally:
            app.state.executor.shutdown(wait=True)

    app = FastAPI(title="efinance 查询", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authentication(request, call_next):
        if auth:
            valid = False
            header = request.headers.get("authorization", "")
            try:
                scheme, encoded = header.split(" ", 1)
                supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
                user, secret = supplied.split(":", 1)
                valid = scheme.lower() == "basic" and hmac.compare_digest(user.encode(), username.encode()) and hmac.compare_digest(secret.encode(), password.encode())
            except (ValueError, UnicodeError, binascii.Error):
                pass
            if not valid:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="efinance", charset="UTF-8"'})
        return await call_next(request)

    @app.exception_handler(EfinanceError)
    async def errors(request, error):
        status = 502
        if isinstance(error, ParameterError):
            status = 422
        elif isinstance(error, SymbolNotFoundError):
            status = 404
        elif isinstance(error, RateLimitError):
            status = 429
        elif isinstance(error, TimeoutError):
            status = 504
        return JSONResponse(dict(error=dict(type=type(error).__name__, message=str(error), attempts=error.attempts)), status_code=status)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    async def execute(request, kind):
        body_bytes = await request.body()
        if len(body_bytes) > 16384:
            raise ParameterError("request body is too large")
        try:
            body = await request.json()
        except ValueError as cause:
            raise ParameterError("request must contain valid JSON") from cause
        if not isinstance(body, dict) or set(body) - {"asset", "codes", "beg", "end", "klt", "fqt", "kind"}:
            raise ParameterError("unexpected query fields")
        if "kind" in body and body["kind"] != kind:
            raise ParameterError("query kind does not match endpoint")
        body.pop("kind", None)
        if not isinstance(body.get("codes"), list) or not 1 <= len(body["codes"]) <= 10:
            raise ParameterError("query requires 1 to 10 codes")
        query = normalize_query(kind=kind, **dict(dict(asset="stock"), **body))
        kwargs = {key: value for key, value in query.items() if key != "codes"}

        def work():
            # One web job executes its symbols sequentially; process transport limits still apply.
            return [(code, query_one(code, **kwargs)) for code in query["codes"]]

        async with app.state.capacity:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(app.state.executor, work)
            try:
                results = await asyncio.shield(future)
            except asyncio.CancelledError:
                # A disconnected client cannot release capacity while its thread is still running.
                await future
                raise
        return query, results

    @app.post("/api/quote")
    async def quote(request: Request):
        query, results = await execute(request, "quote")
        return {"results": [dict(code=code, **table_payload(frame, query)) for code, frame in results]}

    @app.post("/api/history")
    async def history(request: Request):
        query, results = await execute(request, "history")
        return {"results": [dict(code=code, **table_payload(frame, query)) for code, frame in results]}

    @app.post("/api/export")
    async def export(request: Request):
        try:
            body = await request.json()
        except ValueError as cause:
            raise ParameterError("request must contain valid JSON") from cause
        if not isinstance(body, dict):
            raise ParameterError("query must be an object")
        query, results = await execute(request, body.get("kind", "history"))
        # Different assets may have different schemas; one request always has one asset.
        import pandas as pd
        frames = []
        for code, frame in results:
            frame = frame.copy()
            frame.insert(0, "查询标的", code)
            frames.append(frame)
        combined = pd.concat(frames, ignore_index=True)
        return StreamingResponse(csv_chunks(combined), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": 'attachment; filename="efinance.csv"'})

    @app.get("/static/plotly.min.js")
    def plotly_bundle():
        from plotly.offline import get_plotlyjs
        return Response(get_plotlyjs(), media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(STATIC / "index.html"))

    return app


def serve(host="127.0.0.1", port=8000):
    import uvicorn
    if not 1 <= port <= 65535:
        raise ParameterError("port must be between 1 and 65535")
    app = create_app(require_auth=host not in ("127.0.0.1", "localhost", "::1"))
    uvicorn.run(app, host=host, port=port, workers=1)
