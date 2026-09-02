"""In-app "Ask" box: natural-language questions answered by Claude over the shared query tools.

Requires a signed-in user. Uses the single admin-set Anthropic key
(``app_settings.anthropic_api_key_global``); returns 400 if the admin hasn't configured one.
"""
from __future__ import annotations

import json
from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...app_settings import KEY_ANTHROPIC, get_setting
from ...models import AskMessage, User
from ...query import TOOL_SPECS, run_tool
from ...util import current_season
from ..deps import get_session, require_user
from ..schemas import AskIn, AskMessageOut, AskOut

router = APIRouter(tags=["ask"])

# Haiku is the cheapest tier and handles this bounded, tool-driven Q&A well; bump to
# "claude-sonnet-5" if answer quality on multi-step questions needs it.
ASK_MODEL = "claude-haiku-4-5-20251001"
_MAX_TURNS = 6
_MAX_HISTORY = 12  # cap carried-over conversation turns to bound token cost


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
    today = _date.today().isoformat()  # noqa: DTZ011 — coarse local calendar day for the prompt
    system = (
        "You are VBallr's NCAA Division I women's volleyball stats assistant. Answer using ONLY the "
        f"provided tools; never invent numbers. Today is {today} and the current "
        f"season is {season} (pass it as the 'season' argument unless the user names another). When a "
        "question references a weekday or a relative day (e.g. 'today', 'this Friday'), resolve it to "
        "a YYYY-MM-DD date yourself before calling a date-based tool. Be concise: lead with the direct "
        "answer, then a short supporting list if helpful."
    )
    # Rebuild the conversation from this user's stored thread (text turns only — tool_use/
    # tool_result blocks are re-derived per request), then append the new question.
    prior = db.scalars(
        select(AskMessage).where(AskMessage.user_id == user.id).order_by(AskMessage.id)
    ).all()
    messages: list[dict] = []
    for turn in prior[-_MAX_HISTORY:]:
        role = turn.role if turn.role in ("user", "assistant") else "user"
        text = (turn.content or "").strip()
        if not text:
            continue
        # Anthropic requires alternating roles; collapse any accidental repeats into one turn.
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": role, "content": text})
    # History must end on an assistant turn so the new user question keeps the alternation valid.
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + body.question
    else:
        messages.append({"role": "user", "content": body.question})
    tools_used: list[str] = []
    answer = "I couldn't complete that request."

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
            answer = "".join(b.text for b in resp.content if b.type == "text").strip() or "(no answer)"
            break
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI request failed: {e}")

    # Persist this turn (question + answer) so the thread survives reloads and follows the account.
    db.add(AskMessage(user_id=user.id, role="user", content=body.question))
    db.add(AskMessage(user_id=user.id, role="assistant", content=answer, tools=tools_used or None))
    db.commit()
    return AskOut(answer=answer, tools_used=tools_used)


@router.get("/ask/history", response_model=list[AskMessageOut])
def ask_history(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> list[AskMessage]:
    """This user's ongoing Ask conversation, oldest first."""
    return db.scalars(
        select(AskMessage).where(AskMessage.user_id == user.id).order_by(AskMessage.id)
    ).all()


@router.delete("/ask/history", status_code=status.HTTP_204_NO_CONTENT)
def clear_ask_history(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> None:
    """Start a new conversation: delete all of this user's Ask messages."""
    db.execute(delete(AskMessage).where(AskMessage.user_id == user.id))
    db.commit()
