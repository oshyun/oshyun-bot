"""usage_pricing — 토큰 가격 계산, 토큰 사용량 포맷팅 및 AGY Quota 리포트 헬퍼."""

from __future__ import annotations

import asyncio
import datetime
from datetime import timedelta as _timedelta, timezone as _timezone
import json
import os
import shutil

_HOME = os.path.expanduser("~")

# token 가격표 (USD per 1M tokens) — 모델명 prefix 매칭
_MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic Claude
    "claude-opus-5":    {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4":    {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-5":  {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-sonnet-4":  {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-fable-5":   {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "claude-haiku-4":   {"input":  0.80, "output":  4.00, "cache_write":  1.00, "cache_read": 0.08},
    # DeepSeek V4 (공식 API, 비高峰시간)
    "deepseek-v4-flash": {"input": 0.14,  "output": 0.28,  "cache_write": 0.14,  "cache_read": 0.0028},
    "deepseek-v4-pro":   {"input": 0.435, "output": 0.87,  "cache_write": 0.435, "cache_read": 0.003625},
    "deepseek-v4":       {"input": 0.14,  "output": 0.28,  "cache_write": 0.14,  "cache_read": 0.0028},  # fallback (flash 기준)
    # Google Gemini (AGY)
    "gemini-4-argon":                 {"input": 0.20,  "output": 0.80,  "cache_write": 0.20,  "cache_read": 0.05},
    "gemini-4-hydrogen":              {"input": 0.20,  "output": 0.80,  "cache_write": 0.20,  "cache_read": 0.05},
    "gemini-pro-orchestrator-g1-pro": {"input": 1.25,  "output": 5.00,  "cache_write": 1.25,  "cache_read": 0.3125},
    "gemini-3.8-flash":               {"input": 0.15,  "output": 0.60,  "cache_write": 0.15,  "cache_read": 0.0375},
    "gemini-3.7-flash":  {"input": 0.15,  "output": 0.60,  "cache_write": 0.15,  "cache_read": 0.0375},
    "gemini-3.6-flash":  {"input": 0.15,  "output": 0.60,  "cache_write": 0.15,  "cache_read": 0.0375},
    "gemini-3.1-pro":    {"input": 1.25,  "output": 5.00,  "cache_write": 1.25,  "cache_read": 0.3125},
    # GPT OSS
    "gpt-oss-120b":      {"input": 0.15,  "output": 0.60,  "cache_write": 0.15,  "cache_read": 0.0375},
}


def fmt_cost(cost: float | None) -> str:
    """비용(USD) 표시 문자열 반환."""
    return f" (${cost:.4f})" if cost is not None else ""


def _is_deepseek_peak_hour() -> bool:
    """한국 시간 기준 DeepSeek 할증 시간 여부 (10:00~13:00, 15:00~19:00)."""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    hour = now.hour
    return (10 <= hour < 13) or (15 <= hour < 19)


def _calc_cost(model: str | None, usage: dict) -> float:
    """토큰 개수로 직접 비용(USD) 계산. 가격표에 없으면 서버 costUSD 를 fallback."""
    if not model or not usage:
        return 0.0

    pricing = None
    for prefix, p in _MODEL_PRICING.items():
        if model.startswith(prefix):
            pricing = p
            break

    if pricing is None:
        return usage.get("costUSD") or usage.get("cost_usd") or 0.0

    peak = _is_deepseek_peak_hour() and model.startswith("deepseek")
    input_price = pricing["input"] * (2 if peak else 1)
    output_price = pricing["output"] * (2 if peak else 1)
    cache_write_price = pricing.get("cache_write", pricing["input"]) * (2 if peak else 1)
    cache_read_price = pricing.get("cache_read", 0) * (2 if peak else 1)

    in_tokens = usage.get("inputTokens") or usage.get("input_tokens") or 0
    out_tokens = usage.get("outputTokens") or usage.get("output_tokens") or 0
    cache_write_tokens = (
        usage.get("cacheCreationInputTokens")
        or usage.get("cache_write_tokens")
        or 0
    )
    cache_read_tokens = (
        usage.get("cacheReadInputTokens")
        or usage.get("cache_read_tokens")
        or 0
    )

    return (
        in_tokens * input_price / 1_000_000
        + out_tokens * output_price / 1_000_000
        + cache_write_tokens * cache_write_price / 1_000_000
        + cache_read_tokens * cache_read_price / 1_000_000
    )


def format_token_usage(usage: dict | None) -> str:
    """usage 딕셔너리로부터 읽기 쉬운 토큰 소비 요약 문자열을 생성한다."""
    if not usage:
        return "토큰 정보 없음"

    in_tokens = usage.get("inputTokens") or usage.get("input_tokens") or 0
    out_tokens = usage.get("outputTokens") or usage.get("output_tokens") or 0
    thinking_tokens = usage.get("thinkingTokens") or usage.get("thinking_tokens") or 0
    cache_read = usage.get("cacheReadInputTokens") or usage.get("cache_read_tokens") or 0
    total = usage.get("total_tokens") or (in_tokens + out_tokens + thinking_tokens + cache_read)

    parts = []
    if total:
        parts.append(f"총 {total:,} tokens")

    detail = []
    if in_tokens:
        detail.append(f"입력 {in_tokens:,}")
    if out_tokens:
        detail.append(f"출력 {out_tokens:,}")
    if thinking_tokens:
        detail.append(f"사고 {thinking_tokens:,}")
    if cache_read:
        detail.append(f"캐시 {cache_read:,}")

    if detail:
        det_str = f" ({', '.join(detail)})"
        return f"{parts[0] if parts else ''}{det_str}".strip()
    return f"{total:,} tokens" if total else "0 tokens"


def _resolve_agy_bin() -> str:
    """agy 실행 파일의 실제 경로."""
    found = shutil.which("agy") or os.path.join(_HOME, ".local", "bin", "agy")
    return os.path.realpath(found)


def _format_agy_usage_json(raw: str) -> str:
    """agy -p /usage --output-format json 의 출력을 텔레그램 친화적 마크다운으로 포맷팅한다."""
    try:
        res = json.loads(raw)
    except Exception:
        return f"📊 **AGY 사용량 리포트**\n\n```\n{raw}\n```"

    data = res.get("command", {}).get("data", {})
    groups = data.get("groups", [])
    if not groups:
        resp_text = res.get("response") or res.get("result", {}).get("response") or raw
        return f"📊 **AGY 사용량 리포트**\n\n```\n{str(resp_text).strip()}\n```"

    kst = _timezone(_timedelta(hours=9))
    now = datetime.datetime.now(_timezone.utc)

    def _format_rel_time(dt: datetime.datetime) -> str:
        diff = int((dt - now).total_seconds())
        if diff <= 0:
            return "곧 리셋"
        d, rem = divmod(diff, 86400)
        h, m = divmod(rem, 3600)
        if d > 0:
            return f"{d}일 {h}시간 후" if h else f"{d}일 후"
        if h > 0:
            mins = m // 60
            return f"{h}시간 {mins}분 후" if mins else f"{h}시간 후"
        mins = max(1, m // 60)
        return f"{mins}분 후"

    lines = ["📊 **AGY 사용량 리포트 (Quota)**\n"]
    for g in groups:
        g_name = g.get("name", "기타")
        lines.append(f"🔹 **{g_name}**")
        for b in g.get("buckets", []):
            b_name = b.get("name", "")
            if "weekly" in b_name.lower():
                b_label = "주간 한도"
            elif "five hour" in b_name.lower() or b.get("window") == "5h":
                b_label = "5시간 한도"
            else:
                b_label = b_name

            if b.get("disabled"):
                lines.append(f"• {b_label}: ⚪ 비활성 (주간 한도 소진)")
                continue

            frac = b.get("remaining_fraction")
            pct = int(round(frac * 100)) if frac is not None else 0
            icon = "🟢" if pct >= 50 else ("🟡" if pct >= 20 else "🔴")
            reset_raw = b.get("reset_time")
            if reset_raw:
                try:
                    dt = datetime.datetime.fromisoformat(reset_raw.replace("Z", "+00:00"))
                    time_str = dt.astimezone(kst).strftime("%m/%d %H:%M KST")
                    rel_str = _format_rel_time(dt)
                    lines.append(
                        f"• {b_label}: {icon} **{pct}%** (초기화: {time_str}, {rel_str})"
                    )
                except Exception:
                    lines.append(f"• {b_label}: {icon} **{pct}%**")
            else:
                lines.append(f"• {b_label}: {icon} **{pct}%**")
        lines.append("")

    return "\n".join(lines).strip()


async def get_agy_usage_data(timeout: int = 30) -> tuple[dict | None, str]:
    """agy CLI의 /usage 명령을 호출해 JSON 결과 딕셔너리와 원본 문자열을 반환한다."""
    agy_bin = _resolve_agy_bin()
    if not os.path.exists(agy_bin):
        return None, "⚠️ agy 실행 파일을 찾을 수 없습니다."

    cmd = [
        agy_bin,
        "-p",
        "/usage",
        "--output-format",
        "json",
        "--project",
        "oshyun-bot",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        return None, f"⏱ AGY 사용량 조회 시간 초과 ({timeout}s)"
    except Exception as exc:
        return None, f"⚠️ AGY 사용량 조회 오류: {exc}"

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", "replace").strip()
        return None, f"⚠️ AGY 사용량 조회 실패 (코드 {proc.returncode}): {err_msg}"

    raw = stdout.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw)
        return data, raw
    except Exception:
        return None, raw


async def get_agy_usage_report(timeout: int = 30) -> str:
    """agy CLI의 /usage 명령을 호출해 현재 모델 쿼터 및 잔여량을 조회하고 포맷팅해 반환한다."""
    data, raw = await get_agy_usage_data(timeout=timeout)
    if data is None and raw.startswith(("⚠️", "⏱")):
        return raw
    return _format_agy_usage_json(raw)
