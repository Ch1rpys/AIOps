import logging
import os
from pathlib import Path

import pymysql
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="隔离故障实验应用")
logger = logging.getLogger("demo-app")
TIMEOUT = 2
MODE_FILE = os.environ.get("APP_MODE_FILE", "")


def fixed_http_500() -> bool:
    """只认控制文件里的固定标记。读不到文件就按正常应答。"""
    if not MODE_FILE:
        return False
    try:
        return Path(MODE_FILE).read_text(encoding="utf-8").strip() == "http_500"
    except OSError:
        return False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/hello")
def hello():
    if fixed_http_500():
        return JSONResponse(status_code=500, content={"status": "error", "code": "FIXED_500"})
    return {"message": "hello from demo-app"}


@app.get("/db-check")
def db_check():
    try:
        with pymysql.connect(
            host=os.environ["MYSQL_HOST"],
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user="demo",
            password=os.environ["DEMO_MYSQL_PASSWORD"],
            database="demo",
            connect_timeout=TIMEOUT,
            read_timeout=TIMEOUT,
            write_timeout=TIMEOUT,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                value = cursor.fetchone()
        if value != (1,):
            return JSONResponse(status_code=503, content={"status": "error", "code": "SQL_VALUE"})
        return {"status": "ok", "value": 1}
    except pymysql.MySQLError as error:
        code = error.args[0] if error.args else "MYSQL_ERROR"
        logger.warning("db-check failed mysql_error=%s", code)
        return JSONResponse(status_code=503, content={"status": "error", "code": code})


@app.get("/cache-check")
def cache_check():
    try:
        with redis.Redis(
            host=os.environ["REDIS_HOST"], socket_connect_timeout=TIMEOUT, socket_timeout=TIMEOUT
        ) as connection:
            connection.ping()
            connection.set("aiops:app-health", "probe", ex=30)
            value = connection.get("aiops:app-health")
        if value != b"probe":
            return JSONResponse(status_code=503, content={"status": "error", "code": "CACHE_VALUE"})
        return {"status": "ok", "value": "probe"}
    except redis.RedisError as error:
        logger.warning("cache-check failed type=%s", type(error).__name__)
        return JSONResponse(status_code=503, content={"status": "error", "code": "REDIS_ERROR"})
