"""修复动作目录。

模型只能选择目录里的编号。编号在这里编译成固定操作，不能是模型写来的命令。
目录里的另一条路不是非法动作；复测通过才算修好，复测失败只说明这条路没修好。
"""

import re
import unicodedata
from dataclasses import dataclass

from app.config import CHECK_IDS, SERVICES, DomainError

MYSQL_STATEMENTS = {
    "select_1": "SELECT 1",
    "select_database": "SELECT DATABASE()",
    "select_user": "SELECT USER()",
    "select_version": "SELECT VERSION()",
    "show_status": "SHOW GLOBAL STATUS",
    "show_variables": "SHOW GLOBAL VARIABLES",
    "show_processlist": "SHOW PROCESSLIST",
    "show_databases": "SHOW DATABASES",
    "show_tables": "SHOW TABLES",
    "show_grants": "SHOW GRANTS",
    "flush_privileges": "FLUSH PRIVILEGES",
    "flush_status": "FLUSH STATUS",
}

REDIS_OPERATIONS = (
    "ping",
    "dbsize",
    "info",
    "get_health",
    "set_health",
    "del_health",
    "flushdb",
)


@dataclass(frozen=True)
class CatalogAction:
    action_id: str
    target: str
    kind: str
    summary: str
    base: str = ""
    verb: str = ""
    connect_timeout: int = 0
    read_timeout: int = 0
    send_timeout: int = 0
    statement_id: str = ""
    redis_op: str = ""
    net_op: str = ""
    mode: str = ""
    profile: str = ""
    probe: str = ""
    cpus: str = ""
    memory: str = ""
    delay_ms: int = 0
    loss_pct: int = 0
    max_connections: int = 0
    maxmemory: str = ""
    maxmemory_policy: str = ""
    scramble: bool = False


def _life(service: str, verb: str) -> CatalogAction:
    action_id = f"LIFE_{service.replace('-', '_').upper()}_{verb.upper()}"
    return CatalogAction(
        action_id=action_id,
        target=service,
        kind="life",
        verb=verb,
        summary=f"{verb} {service}",
    )


def _nginx(connect: int, read: int, send: int) -> CatalogAction:
    action_id = f"NGINX_T{connect}_{read}_{send}"
    return CatalogAction(
        action_id=action_id,
        target="demo-nginx",
        kind="nginx_timeout",
        connect_timeout=connect,
        read_timeout=read,
        send_timeout=send,
        summary=f"Nginx 仍指向 demo-app，超时 {connect}/{read}/{send} 秒",
    )


def _build() -> dict[str, CatalogAction]:
    catalog: dict[str, CatalogAction] = {}

    def add(entry: CatalogAction) -> None:
        catalog[entry.action_id] = entry

    add(
        CatalogAction(
            "NO_ACTION",
            "none",
            "none",
            "不改容器，也不改配置",
        )
    )
    legacy = {
        "START_DEMO_MYSQL": ("demo-mysql", "start"),
        "START_DEMO_REDIS": ("demo-redis", "start"),
        "START_DEMO_APP": ("demo-app", "start"),
    }
    for action_id, (service, verb) in legacy.items():
        add(
            CatalogAction(
                action_id,
                service,
                "life",
                verb=verb,
                summary=f"{verb} {service}",
            )
        )
    add(
        CatalogAction(
            "RESTORE_NGINX_UPSTREAM",
            "demo-nginx",
            "nginx_profile",
            profile="healthy",
            summary="换回固定的健康 Nginx 配置并重载",
        )
    )
    add(
        CatalogAction(
            "RESTORE_APP_MODE",
            "demo-app",
            "app_mode",
            mode="ok",
            summary="把应用从固定 500 改回正常响应",
        )
    )
    for service in SERVICES:
        for verb in ("start", "stop", "restart", "recreate", "pause", "unpause", "kill"):
            add(_life(service, verb))
    for connect in (1, 2, 3, 5, 8, 10, 15):
        for read in (2, 3, 5, 8, 10, 15, 30):
            for send in (2, 3, 5, 8, 15, 30):
                add(_nginx(connect, read, send))
    for statement_id in MYSQL_STATEMENTS:
        add(
            CatalogAction(
                f"MYSQL_{statement_id.upper()}",
                "demo-mysql",
                "mysql",
                statement_id=statement_id,
                summary=f"在隔离库执行已登记语句 {statement_id}",
            )
        )
    add(
        CatalogAction(
            "MYSQL_RESET_DEMO_PASSWORD",
            "demo-mysql",
            "mysql_password",
            summary="把实验账号密码改回本机配置的实验密码",
        )
    )
    for operation in REDIS_OPERATIONS:
        add(
            CatalogAction(
                f"REDIS_{operation.upper()}",
                "demo-redis",
                "redis",
                redis_op=operation,
                summary=f"对隔离 Redis 执行已登记操作 {operation}",
            )
        )
    for service in SERVICES:
        for net_op in ("connect", "disconnect"):
            add(
                CatalogAction(
                    f"NET_{net_op.upper()}_{service.replace('-', '_').upper()}",
                    service,
                    "net",
                    net_op=net_op,
                    summary=f"把 {service} {net_op} 到演示网络",
                )
            )
    add(
        CatalogAction(
            "MYSQL_SCRAMBLE_DEMO_PASSWORD",
            "demo-mysql",
            "mysql_password_scramble",
            scramble=True,
            summary="把实验账号改成仓库内固定乱码",
        )
    )
    for service in SERVICES:
        for cpus in ("0.1", "0.2", "0.5"):
            add(
                CatalogAction(
                    f"CPU_{service.replace('-', '_').upper()}_{cpus.replace('.', '_')}",
                    service,
                    "cpu_limit",
                    cpus=cpus,
                    summary=f"限制 {service} CPU 为 {cpus}",
                )
            )
        add(
            CatalogAction(
                f"CPU_{service.replace('-', '_').upper()}_RESET",
                service,
                "cpu_limit",
                cpus="0",
                summary=f"取消 {service} 的 CPU 限制",
            )
        )
        for memory in ("32m", "64m", "128m"):
            add(
                CatalogAction(
                    f"MEM_{service.replace('-', '_').upper()}_{memory.upper()}",
                    service,
                    "mem_limit",
                    memory=memory,
                    summary=f"限制 {service} 内存为 {memory}",
                )
            )
        add(
            CatalogAction(
                f"MEM_{service.replace('-', '_').upper()}_RESET",
                service,
                "mem_limit",
                memory="0",
                summary=f"取消 {service} 的内存限制",
            )
        )
        for delay in (50, 100, 200, 500):
            add(
                CatalogAction(
                    f"NETEM_DELAY_{service.replace('-', '_').upper()}_{delay}",
                    service,
                    "netem_delay",
                    delay_ms=delay,
                    summary=f"{service} 延迟 {delay}ms",
                )
            )
        for loss in (5, 10, 20):
            add(
                CatalogAction(
                    f"NETEM_LOSS_{service.replace('-', '_').upper()}_{loss}",
                    service,
                    "netem_loss",
                    loss_pct=loss,
                    summary=f"{service} 丢包 {loss}%",
                )
            )
        add(
            CatalogAction(
                f"NETEM_CLEAR_{service.replace('-', '_').upper()}",
                service,
                "netem_clear",
                summary=f"清除 {service} 的 netem",
            )
        )
    for value in (2, 5, 10, 151):
        add(
            CatalogAction(
                f"MYSQL_MAX_CONNECTIONS_{value}",
                "demo-mysql",
                "mysql_max_connections",
                max_connections=value,
                summary=f"SET GLOBAL max_connections={value}",
            )
        )
    for value in ("1mb", "4mb", "16mb", "0"):
        token = value.upper()
        add(
            CatalogAction(
                f"REDIS_MAXMEMORY_{token}",
                "demo-redis",
                "redis_maxmemory",
                maxmemory=value,
                summary=f"CONFIG SET maxmemory {value}",
            )
        )
    for policy in ("noeviction", "allkeys-lru"):
        add(
            CatalogAction(
                f"REDIS_POLICY_{policy.upper().replace('-', '_')}",
                "demo-redis",
                "redis_maxmemory_policy",
                maxmemory_policy=policy,
                summary=f"CONFIG SET maxmemory-policy {policy}",
            )
        )
    for mode in ("slow", "empty", "wrong_json"):
        add(
            CatalogAction(
                f"APP_MODE_{mode.upper()}",
                "demo-app",
                "app_mode",
                mode=mode,
                summary=f"把应用模式写成 {mode}",
            )
        )
    for profile in ("mysql", "blackhole"):
        add(
            CatalogAction(
                f"NGINX_UPSTREAM_{profile.upper()}",
                "demo-nginx",
                "nginx_profile",
                profile=profile,
                summary=f"换成固定的 {profile} Nginx 上游",
            )
        )
    bases = [entry for entry in catalog.values() if entry.kind != "none"]
    for entry in bases:
        for probe in CHECK_IDS:
            add(
                CatalogAction(
                    f"{entry.action_id}__THEN__{probe.upper()}",
                    entry.target,
                    "then",
                    base=entry.action_id,
                    probe=probe,
                    summary=f"{entry.summary}，随后用 {probe} 判断",
                )
            )
    return catalog


CATALOG = _build()
_COMPACT = {re.sub(r"[\s_\-]+", "", key).upper(): key for key in CATALOG}
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060\u00ad]")
_PUNCT = re.compile(r"[`\"'“”‘’「」『』，,、;；。.!！?？()（）\[\]【】{}｛｝:：/=&|<>~@#$^*+\\]")
_VERBS = {"start", "stop", "restart", "recreate", "pause", "unpause", "kill", "up"}
_VERB_ALIASES = {
    "重新启动": "restart",
    "取消暂停": "unpause",
    "强制重建": "recreate",
    "重新创建": "recreate",
    "起起来": "start",
    "拉起来": "start",
    "reboot": "restart",
    "reload": "restart",
    "recover": "start",
    "fix": "start",
    "重启": "restart",
    "重建": "recreate",
    "启动": "start",
    "开启": "start",
    "开机": "start",
    "拉起": "start",
    "停止": "stop",
    "关掉": "stop",
    "关闭": "stop",
    "关机": "stop",
    "暂停": "pause",
    "继续": "unpause",
    "杀死": "kill",
    "杀掉": "kill",
    "强杀": "kill",
    "恢复": "start",
    "修复": "start",
}
_SERVICES = {
    "demomysql": "demo-mysql",
    "database": "demo-mysql",
    "mysql": "demo-mysql",
    "数据库": "demo-mysql",
    "demoredis": "demo-redis",
    "redis": "demo-redis",
    "cache": "demo-redis",
    "缓存": "demo-redis",
    "demoapp": "demo-app",
    "app": "demo-app",
    "应用": "demo-app",
    "demonginx": "demo-nginx",
    "nginx": "demo-nginx",
    "proxy": "demo-nginx",
    "代理": "demo-nginx",
}
# 太短的英文别名不能做子串：apply/mapping 里都有 app。
_SERVICE_SHORT = {"app": "demo-app", "db": "demo-mysql"}
_CHECKS = {re.sub(r"[\s_\-]+", "", check).lower(): check for check in CHECK_IDS}
_CHECKS.update(
    {
        "select1": "mysql_select_1",
        "检查数据库": "mysql_select_1",
        "数据库检查": "app_db_check",
        "健康检查": "app_health",
        "apphealth": "app_health",
    }
)
_DOCKER_NOISE = {
    "docker",
    "docker-compose",
    "compose",
    "sudo",
    "--context",
    "default",
    "-p",
    "aiops-demo",
    "--project-name",
    "exec",
    "cli",
    "the",
    "a",
    "an",
    "please",
    "container",
    "service",
    "服务",
    "容器",
    "进程",
}
_NEGATION = re.compile(
    r"不要|不应该|不能|勿|禁止|(?:^|[^\u4e00-\u9fff])别|"
    r"\bdo\s+not\b|\bdon't\b|\bdont\b|\bcan't\b|\bcant\b|\bcannot\b|"
    r"\bshouldn't\b|\bshouldnt\b|\bwon't\b|\bwont\b|\bnever\b|\bnot\b",
    re.IGNORECASE,
)
_RECREATE = re.compile(r"force[\s\-]*recreate|强制重建|重新创建", re.IGNORECASE)
_NO_ACTION = re.compile(
    r"什么都不做|无需操作|无需处理|观察即可|不执行|do\s+nothing|\bno\s*op\b|\bnoop\b",
    re.IGNORECASE,
)
_THEN_MARKERS = (
    "然后检查",
    "followed by",
    "and then",
    "接着检查",
    "再检查",
    "随后",
    "然后",
    "接着",
    "then",
    "&&",
    "->",
    "→",
)
_GENERIC_REDIS = {"ping", "info"}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _prepare(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _INVISIBLE.sub("", cleaned)
    cleaned = re.sub(r"&&|->|→", " then ", cleaned)
    cleaned = re.sub(r"(\d+)\.0+\b", r"\1", cleaned)
    cleaned = _PUNCT.sub(" ", cleaned)
    return _collapse(cleaned)


def _compact(text: str) -> str:
    return re.sub(r"[\s_\-]+", "", text)


def _is_negated(text: str) -> bool:
    if _NEGATION.search(text):
        return True
    prepared = _prepare(text)
    return bool(prepared) and bool(_NEGATION.search(prepared))


def _service_name(token: str) -> str | None:
    compact = _compact(token).lower()
    compact = re.sub(r"^aiopsdemo", "", compact)
    compact = re.sub(r"\d+$", "", compact)
    if compact in _SERVICE_SHORT:
        return _SERVICE_SHORT[compact]
    return _SERVICES.get(compact)


def _services_in(text: str) -> list[str]:
    found: list[str] = []
    pattern = r"aiops-demo-(demo-(?:mysql|redis|app|nginx))(?:-\d+)?"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        found.append(match.group(1).lower())
    compact = _compact(text).lower()
    for alias, service in _SERVICES.items():
        if alias in _SERVICE_SHORT:
            continue
        if alias.isascii() and alias in compact:
            found.append(service)
        elif not alias.isascii() and alias in text:
            found.append(service)
    lowered = text.lower()
    for alias, service in _SERVICE_SHORT.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            found.append(service)
    ordered: list[str] = []
    for service in found:
        if service not in ordered:
            ordered.append(service)
    return ordered


def _check_name(text: str) -> str | None:
    prepared = _prepare(text) if text else ""
    compact = _compact(prepared).lower()
    direct = _CHECKS.get(compact) or _CHECKS.get(prepared)
    if direct:
        return direct
    best = ""
    best_len = 0
    for key, check in _CHECKS.items():
        if len(key) >= 7 and key in compact and len(key) > best_len:
            best = check
            best_len = len(key)
    return best or None


def canonical_target(text: str) -> str:
    prepared = _prepare(text)
    services = _services_in(prepared)
    if len(services) == 1:
        return services[0]
    compact = _compact(prepared).lower()
    if compact in {"none", "no"}:
        return "none"
    return _collapse(prepared)


def canonical_check(text: str) -> str:
    return _check_name(text) or _collapse(_prepare(text))


def _verb_in(text: str) -> str:
    lower = text.lower()
    if _RECREATE.search(text):
        return "recreate"
    for raw, verb in sorted(_VERB_ALIASES.items(), key=lambda item: -len(item[0])):
        if raw.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(raw)}(?![a-z0-9])", lower):
                return verb
        elif raw in text:
            return verb
    for token in text.split():
        word = token.lower()
        if word == "up":
            return "start"
        if word in _VERBS and word != "up":
            return word
    return ""


def _numbers(text: str) -> list[int]:
    numbers: list[int] = []
    pattern = r"(\d+)(?:\.0+)?\s*(ms|毫秒|s|秒|sec|secs|second|seconds)?"
    for match in re.finditer(pattern, text, re.IGNORECASE):
        value = int(match.group(1))
        unit = (match.group(2) or "").lower()
        if unit in {"ms", "毫秒"}:
            if value % 1000:
                continue
            value //= 1000
        if value:
            numbers.append(value)
    return numbers


def _mentions_timeout(text: str) -> bool:
    lower = text.lower()
    if any(word in lower for word in ("nginx", "proxy", "timeout", "upstream")):
        return True
    if "代理" in text or "超时" in text:
        return True
    return bool(re.search(r"connect.+\bread\b.+\bsend\b", lower, re.DOTALL))


def _split_then(text: str) -> tuple[str, str]:
    lower = text.lower()
    hits: list[tuple[int, int]] = []
    for marker in _THEN_MARKERS:
        if marker in {"&&", "->", "→"}:
            index = lower.find(marker) if marker.isascii() else text.find(marker)
        elif marker.isascii():
            matched = re.search(rf"\b{re.escape(marker)}\b", lower)
            index = matched.start() if matched else -1
        else:
            index = text.find(marker)
        if index >= 0:
            hits.append((index, len(marker)))
    if not hits:
        return text, ""
    hits.sort(key=lambda item: (item[0], -item[1]))
    index, length = hits[0]
    left, right = text[:index], text[index + length :]
    marker = lower[index : index + length]
    if marker in {"&&", "->", "→"} and not _check_name(right):
        return text, ""
    return left, right


def _id_regex(action_id: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in action_id.split("_")]
    return re.compile(r"(?<![A-Z0-9])" + r"[\s_\-]*".join(parts) + r"(?![A-Z0-9])")


_ID_PATTERNS = [
    (_id_regex(action_id), action_id, len(_compact(action_id)))
    for action_id in CATALOG
    if "__THEN__" not in action_id and len(_compact(action_id)) >= 8
]


def _catalog_in(text: str) -> CatalogAction | None:
    upper = text.upper()
    best_id = ""
    best_len = 0
    for pattern, action_id, compact_len in _ID_PATTERNS:
        if compact_len > best_len and pattern.search(upper):
            best_id = action_id
            best_len = compact_len
    return CATALOG.get(best_id) if best_id else None


def _with_probe(entry: CatalogAction | None, probe: str) -> CatalogAction | None:
    if entry is None:
        return None
    if probe:
        chained = CATALOG.get(f"{entry.action_id}__THEN__{probe.upper()}")
        return chained or entry
    return entry


def _is_restore_nginx(text: str) -> bool:
    if not re.search(r"nginx|代理|upstream", text, re.IGNORECASE):
        return False
    return bool(re.search(r"restore|恢复|reload|reset|改回|健康配置", text, re.IGNORECASE))


def _is_restore_app(text: str) -> bool:
    lower = text.lower()
    if re.search(r"\b500\b", lower) and re.search(
        r"restore|恢复|fix|关闭|改回|正常|mode", text, re.IGNORECASE
    ):
        return True
    has_app = bool(re.search(r"(?<![a-z0-9])app(?![a-z0-9])|应用", lower))
    restored = bool(
        re.search(r"restore|恢复|改回|(?<![a-z])set(?![a-z])|(?<![a-z])fix(?![a-z])", lower)
    )
    healthy = bool(re.search(r"正常|mode|\bok\b|healthy|\b500\b", lower))
    return has_app and restored and healthy


def _is_password_reset(text: str) -> bool:
    return bool(re.search(r"password|密码", text, re.IGNORECASE)) and bool(
        re.search(r"reset|恢复|改回", text, re.IGNORECASE)
    )


def _net_op(text: str) -> str:
    if re.search(r"\bdisconnect\b|断开", text, re.IGNORECASE):
        return "disconnect"
    if re.search(r"\bconnect\b|连回|接入", text, re.IGNORECASE) and re.search(
        r"network|网络", text, re.IGNORECASE
    ):
        return "connect"
    return ""


def _mysql_match(statement: str) -> str:
    folded = _compact(statement).upper()
    relaxed = folded.replace("FULL", "")
    best_id = ""
    best_len = 0
    for statement_id, sql in MYSQL_STATEMENTS.items():
        sql_fold = _compact(sql).upper()
        id_fold = _compact(statement_id).upper()
        matched = sql_fold in folded or sql_fold in relaxed or id_fold == folded
        if not matched:
            tokens = re.findall(r"[A-Z0-9]+", sql_fold)
            matched = len(tokens) >= 2 and all(token in folded for token in tokens)
        if matched and len(sql_fold) > best_len:
            best_id = statement_id
            best_len = len(sql_fold)
    return best_id


def _redis_match(statement: str) -> str:
    folded = _compact(statement).lower()
    best_op = ""
    best_len = 0
    for operation in REDIS_OPERATIONS:
        folded_op = operation.replace("_", "")
        exact = folded in {folded_op, f"redis{folded_op}", f"rediscli{folded_op}"}
        wrapped = folded_op in folded and ("redis" in folded or "cli" in folded)
        prefixed = folded.startswith(folded_op) and operation not in _GENERIC_REDIS
        if operation in _GENERIC_REDIS and "redis" not in folded and "cli" not in folded:
            continue
        if (exact or wrapped or prefixed) and len(folded_op) > best_len:
            best_op = operation
            best_len = len(folded_op)
    return best_op


def _parse_loose(text: str) -> CatalogAction | None:
    action_text, probe_text = _split_then(text)
    probe = _check_name(probe_text) or ""
    if _collapse(probe_text) and not probe and (_services_in(probe_text) or _verb_in(probe_text)):
        return None
    services = _services_in(action_text)
    if len(services) > 1:
        return None
    tokens = [
        token
        for token in action_text.split()
        if token.lower() not in _DOCKER_NOISE and not token.startswith("-")
    ]
    numbers = _numbers(action_text)
    if len(numbers) >= 3 and _mentions_timeout(action_text):
        for candidate in (numbers[-3:], numbers[:3]):
            entry = CATALOG.get(f"NGINX_T{candidate[0]}_{candidate[1]}_{candidate[2]}")
            if entry is not None:
                return _with_probe(entry, probe)
    if _NO_ACTION.search(action_text) and not services:
        return CATALOG["NO_ACTION"]
    if _is_password_reset(action_text):
        return _with_probe(CATALOG.get("MYSQL_RESET_DEMO_PASSWORD"), probe)
    if _is_restore_nginx(action_text):
        return _with_probe(CATALOG.get("RESTORE_NGINX_UPSTREAM"), probe)
    if _is_restore_app(action_text):
        return _with_probe(CATALOG.get("RESTORE_APP_MODE"), probe)
    service = services[0] if services else ""
    net = _net_op(action_text)
    if net and service:
        action = f"NET_{net.upper()}_{service.replace('-', '_').upper()}"
        return _with_probe(CATALOG.get(action), probe)
    buried = _catalog_in(action_text)
    if buried is not None:
        return _with_probe(buried, probe)
    verb = _verb_in(action_text)
    if verb and service:
        entry = CATALOG.get(f"LIFE_{service.replace('-', '_').upper()}_{verb.upper()}")
        return _with_probe(entry, probe)
    statement = " ".join(tokens)
    mysql_id = _mysql_match(statement)
    if mysql_id:
        return _with_probe(CATALOG.get(f"MYSQL_{mysql_id.upper()}"), probe)
    redis_op = _redis_match(statement)
    if redis_op:
        return _with_probe(CATALOG.get(f"REDIS_{redis_op.upper()}"), probe)
    return None


def lookup(action_id: str) -> CatalogAction | None:
    """按目录编号查找。连续空白、下划线和连字符的差别都折叠后再对。"""
    if not isinstance(action_id, str):
        return None
    if _is_negated(action_id):
        return None
    collapsed = _prepare(action_id)
    if not collapsed:
        return None
    direct = CATALOG.get(collapsed)
    if direct is not None:
        return direct
    compact = _COMPACT.get(_compact(collapsed).upper())
    if compact is not None:
        return CATALOG[compact]
    return _parse_loose(collapsed)


def canonical_action_id(text: str) -> str:
    entry = lookup(text)
    return entry.action_id if entry is not None else _collapse(text)


def perform(docker, action_id: str) -> dict:
    entry = lookup(action_id)
    if entry is None:
        raise DomainError("BLOCKED", "拒绝目录之外的动作")
    if entry.kind == "then":
        output = perform(docker, entry.base)
        return {**output, "then_check": entry.probe}
    if entry.kind == "none":
        return {"operation": "NO_ACTION", "exit_code": 0}
    if entry.kind == "life":
        return docker.change(entry.target, entry.verb)
    if entry.kind == "nginx_profile":
        return docker.apply_nginx_upstream(entry.profile)
    if entry.kind == "nginx_timeout":
        return docker.apply_nginx_timeouts(
            entry.connect_timeout, entry.read_timeout, entry.send_timeout
        )
    if entry.kind == "app_mode":
        return docker.apply_app_mode(entry.mode)
    if entry.kind == "mysql":
        return docker.apply_mysql_statement(entry.statement_id)
    if entry.kind == "mysql_password":
        return docker.reset_demo_password()
    if entry.kind == "mysql_password_scramble":
        return docker.scramble_demo_password()
    if entry.kind == "redis":
        return docker.apply_redis_operation(entry.redis_op)
    if entry.kind == "net":
        return docker.network(entry.target, entry.net_op)
    if entry.kind == "cpu_limit":
        return docker.update_resources(entry.target, cpus=entry.cpus)
    if entry.kind == "mem_limit":
        return docker.update_resources(entry.target, memory=entry.memory)
    if entry.kind == "netem_delay":
        return docker.apply_netem(entry.target, delay_ms=entry.delay_ms)
    if entry.kind == "netem_loss":
        return docker.apply_netem(entry.target, loss_pct=entry.loss_pct)
    if entry.kind == "netem_clear":
        return docker.clear_netem(entry.target)
    if entry.kind == "mysql_max_connections":
        return docker.set_mysql_max_connections(entry.max_connections)
    if entry.kind == "redis_maxmemory":
        return docker.set_redis_config("maxmemory", entry.maxmemory)
    if entry.kind == "redis_maxmemory_policy":
        return docker.set_redis_config("maxmemory-policy", entry.maxmemory_policy)
    raise DomainError("BLOCKED", "拒绝目录之外的动作")
