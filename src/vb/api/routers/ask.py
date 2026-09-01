"""In-app "Ask" box: natural-language questions answered by Claude over the shared query tools.

Requires a signed-in user. Uses the single admin-set Anthropic key
(``app_settings.anthropic_api_key_global``); returns 400 if the admin hasn't configured one.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...app_settings import KEY_ANTHROPIC, get_setting
from ...models import User
from ...query import TOOL_SPECS, run_tool
from ...util import current_season
from ..deps import get_session, require_user
from ..schemas import AskIn, AskOut

router = APIRouter(tags=["ask"])

ASK_MODEL = "claude-sonnet-5"
_MAX_TURNS = 6


@router.post("/ask", response_model=AskOut)
def ask(
    body: AskIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> AskOut:
    key = get_setting(db, KEY_ANTHROPIC)
    if not key:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The AI assistant is not configured. An admin must set the Anthropic API key.",
        )
    try:
        import anthropic
    except ImportError:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "anthropic SDK not installed.")

    client = anthropic.Anthropic(api_key=key)
    season = body.season or current_season()
    system = (
        "You are VBallr's NCAA Division I women's volleyball stats assistant. Answer using ONLY the "
        f"provided tools; never invent numbers. The current season is {season} (pass it as the "
        "'season' argument unless the user names another). Be concise: lead with the direct answer, "
        "then a short supporting list if helpful."
    )
    messages: list[dict] = [{"role": "user", "content": body.question}]
    tools_used: list[str] = []

    try:
        for _ in range(_MAX_TURNS):
            resp = client.messages.create(
                model=ASK_MODEL, max_tokens=1024, system=system, tools=TOOL_SPECS, messages=messages,
            )
            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        tools_used.append(block.name)
                        result = run_tool(db, block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": tool_results})
                continue
            answer = "".join(b.text for b in resp.content if b.type == "text").strip()
            return AskOut(answer=answer or "(no answer)", tools_used=tools_used)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI request failed: {e}")

    return AskOut(answer="I couldn't complete that request.", tools_used=tools_used)
