import logging
import os
import sqlite3
from contextlib import ExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from filelock import FileLock
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.agents.evidence import EvidenceAgent
from app.api.routes import router
from app.config import DomainError, Settings
from app.graph.workflow import Workflow
from app.integrations.docker import DockerController
from app.integrations.models import HolmesDiagnosisProvider, LLMPlanner
from app.integrations.probes import Probes
from app.integrations.vision import DashScopeVisionProvider
from app.repositories.store import Store
from app.services.attachments import AttachmentService
from app.services.evaluation import EvaluationService
from app.services.executor import ActionExecutor
from app.services.platform import Platform
from app.services.twin import TwinService
from app.worlds import world_from_settings

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"
ALLOWED_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173"}


def create_app(settings: Settings | None = None, factory=None) -> FastAPI:
    settings = settings or Settings()
    logger = logging.getLogger("aiops")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            # 单进程控制实验与检查点；第二个 worker 必须明确失败。
            stack.enter_context(FileLock(settings.data_dir / "platform.lock", timeout=0))
            client = stack.enter_context(httpx.Client(trust_env=False, follow_redirects=False))
            store = Store(settings.data_dir / "platform.sqlite3")
            stack.callback(store.close)
            connection = sqlite3.connect(
                settings.data_dir / "checkpoints.sqlite3", check_same_thread=False
            )
            stack.callback(connection.close)
            # 显式限制反序列化，避免依赖第三方模块导入时读取环境变量的时机。
            saver = SqliteSaver(
                connection,
                serde=JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None),
            )
            if factory:
                service, evaluation_service = factory(settings, store, saver, client)
            else:
                docker = DockerController(settings)
                probes = Probes(settings, client)
                twin_world = world_from_settings(settings, "twin")
                twin_docker = DockerController(settings, twin_world)
                twin_probes = Probes(settings, client, twin_world)
                twin = TwinService(settings, twin_docker, twin_probes, store)
                evidence = EvidenceAgent(settings, docker, probes)
                provider = HolmesDiagnosisProvider(settings, client)
                planner = LLMPlanner(settings, client)
                workflow = Workflow(
                    store,
                    evidence,
                    provider,
                    planner,
                    ActionExecutor(store, docker),
                    probes,
                    saver,
                    settings,
                    twin=twin,
                )
                service = Platform(
                    settings,
                    store,
                    docker,
                    probes,
                    evidence,
                    workflow,
                    client,
                    attachments=AttachmentService(
                        settings, DashScopeVisionProvider(settings, client)
                    ),
                    twin=twin,
                )
                evaluation_service = EvaluationService(
                    settings,
                    store,
                    docker,
                    probes,
                    evidence,
                    provider,
                    attachments=AttachmentService(
                        settings, DashScopeVisionProvider(settings, client)
                    ),
                    planner=planner,
                    twin=twin,
                )
            app.state.platform = service
            app.state.evaluation = evaluation_service
            service.recover()
            yield

    app = FastAPI(title="AIOps 处置验证平台", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def protect_origin(request: Request, call_next):
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin")
            and request.headers["origin"] not in ALLOWED_ORIGINS
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {"code": "ORIGIN_BLOCKED", "message": "写操作仅接受本机控制台来源"}
                },
            )
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(_request, error):
        return JSONResponse(
            status_code=error.status,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数不符合接口契约",
                    "fields": [".".join(str(p) for p in e["loc"]) for e in error.errors()],
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request, error):
        logging.getLogger("aiops.api").error("api_error type=%s", type(error).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "平台内部异常，请检查本机日志"}
            },
        )

    app.include_router(router)
    return app


app = create_app()
