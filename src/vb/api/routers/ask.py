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
from ..deps import get_session, require_user, require_verified
from ..schemas import AskIn, AskMessageOut, AskOut

router = APIRouter(tags=["ask"])

# Haiku is the cheapest tier and handles this bounded, tool-driven Q&A well; bump to
# "claude-sonnet-5" if answer quality on multi-step questions needs it.
ASK_MODEL = "claude-haiku-4-5-20251001"
_MAX_TURNS = 6
_MAX_HISTORY = 12  # cap carried-over conversation turns to bound token cost
# Names of our local (client-executed) tools; anything else in a response (e.g. web_search) is a
# server-side tool whose result Anthropic returns inline — we must not try to run it ourselves.
run_tool_names = {spec["name"] for spec in TOOL_SPECS}


@router.post("/ask", response_model=AskOut)
def ask(
    body: AskIn,
    user: User = Depends(require_verified),
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
        "You are VBallr's NCAA Division I women's volleyball stats assistant. Stat numbers must come "
        "ONLY from the provided data tools; never invent stats. Today is "
        f"{today} and the current season is {season} (pass it as the 'season' argument unless the "
        "user names another). When a question references a weekday or a relative day (e.g. 'today', "
        "'this Friday'), resolve it to a YYYY-MM-DD date yourself before calling a date-based tool.\n\n"
        "Resolving school & conference names: use your own knowledge to expand abbreviations and "
        "nicknames to the school's real name before calling a tool — e.g. 'IU'→Indiana, 'tOSU'/'Ohio "
        "State'→Ohio State, 'Ole Miss'→Mississippi, 'Bama'→Alabama, 'Pitt'→Pittsburgh, 'MAC'→Mid-"
        "American Conference. If a data tool returns an error like 'no team matched', DON'T give up: "
        "retry with an alternate spelling, or call list_teams (search by a substring, or by "
        "conference) to find the exact name in the database, then call the tool again. Only say you "
        "can't find something after list_teams confirms it isn't in the data.\n\n"
        "You also have web_search for context that isn't in the volleyball database (e.g. what a "
        "conference's abbreviation stands for, a school's location, general background). Prefer the "
        "data tools for any stat, roster, schedule, or standings question. Be concise: lead with the "
        "direct answer, then a short supporting list if helpful."
    )
    # Local data tools + Anthropic's server-side web search (executed on their side; bounded to a
    # few uses to cap cost). Server-tool results come back inline — we don't run them via run_tool.
    tools = [*TOOL_SPECS, {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
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
                model=ASK_MODEL, max_tokens=1024, system=system, tools=tools, messages=messages,
            )
            # pause_turn: a server tool (web search) is mid-flight — echo the partial turn back to
            # let the model continue where it left off.
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason == "tool_use":
                # Only local (client-side) tools need results returned; web_search runs server-side
                # and its result is already inline in resp.content.
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use" and block.name in run_tool_names:
                        tools_used.append(block.name)
                        result = run_tool(db, block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        })
                    elif block.type == "server_tool_use":
                        tools_used.append(block.name)
                messages.append({"role": "assistant", "content": resp.content})
                if tool_results:
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
