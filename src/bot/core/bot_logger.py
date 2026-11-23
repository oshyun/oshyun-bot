"""bot_logger — oshyun-bot 로깅 핸들러 및 Obsidian 볼트 로그 기록 헬퍼."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
import sys

from paths import WEB_LOG_FILE, WORKDIR


def _is_test_runner() -> bool:
    return "unittest" in sys.modules or "pytest" in sys.modules


def _append_log(path: str, entry: str) -> None:
    """지정 경로 파일에 한 줄을 추가하고 최근 100줄만 유지한다."""
    if _is_test_runner() and path in (WEB_LOG_FILE, BOT_LOG_PATH):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a+", encoding="utf-8") as f:
            f.write(entry + "\n")
            f.seek(0)
            lines = f.readlines()
        if len(lines) > 100:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-100:])
    except Exception:
        pass


class CappedFileHandler(logging.Handler):
    """지정 경로 파일에 직접 기록하되 최근 100줄만 유지한다(_append_log 재사용)."""

    def __init__(self, log_path: str = WEB_LOG_FILE) -> None:
        super().__init__()
        self.log_path = log_path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _append_log(self.log_path, self.format(record))
        except Exception:
            self.handleError(record)


_CappedFileHandler = CappedFileHandler  # 하위 호환성 alias

_log_handler = CappedFileHandler(WEB_LOG_FILE)
_log_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("oshyun-bot")


def setup_web_logger() -> logging.Logger:
    """웹 서버 전용 로거 설정 (web-server.log 최근 100줄 유지)."""
    web_logger = logging.getLogger("oshyun-web")
    web_logger.setLevel(logging.INFO)
    has_handler = any(
        isinstance(h, CappedFileHandler) and getattr(h, "log_path", "") == WEB_LOG_FILE
        for h in web_logger.handlers
    )
    if not has_handler:
        handler = CappedFileHandler(WEB_LOG_FILE)
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        web_logger.addHandler(handler)
    return web_logger


# httpx 는 요청 URL(봇 토큰 포함)을 INFO 로 찍는다 — 토큰 유출 방지를 위해 낮춘다.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_LOG_PATH = os.path.join(WORKDIR, os.getenv("BOT_LOG_PATH") or "_bot/bot-log.md")


async def _run_vault_log(entry: str) -> None:
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _append_log, BOT_LOG_PATH, entry)
    except Exception:
        pass


def vault_log(kind: str, text: str) -> None:
    """볼트 bot-log.md 에 한 줄 기록 (fire-and-forget, 실패 무시)."""
    if not WORKDIR:
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"{stamp} | {kind} | {text.replace(chr(10), ' ')}"
    try:
        asyncio.create_task(_run_vault_log(entry))
    except RuntimeError:
        # 이벤트 루프가 아직 돌지 않는 환경(동기/테스트 등) 대비
        pass
