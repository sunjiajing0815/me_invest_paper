"""Three-node news triage graph: classify→critic→arbitrate (conditional)."""

from __future__ import annotations

import json
import logging
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing_extensions import TypedDict

from ..services.llm import HAIKU, SONNET, LLMClient, load_prompt
from ..services.news import NewsRaw
from . import make_checkpointer
from ._nodes import llm_node_call

log = logging.getLogger(__name__)

MAX_HEADLINES_PER_BATCH = 20


# ---------------------------------------------------------------------------
# Pydantic schemas for LLM outputs
# ---------------------------------------------------------------------------


class NewsTriageItem(BaseModel):
    url_hash: str
    is_material: bool
    sentiment: Literal["bullish", "bearish", "neutral"] | None = None
    summary: str | None = None


class NewsTriageBatch(BaseModel):
    items: list[NewsTriageItem]


class NewsCriticReview(BaseModel):
    flagged: list[str]  # url_hashes the critic wants re-evaluated


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------


class NewsTriageState(TypedDict):
    ticker: str
    raws: list[NewsRaw]
    classifications: list[NewsTriageItem]
    flagged: list[str]
    final: list[NewsTriageItem]
    telemetry: dict[str, object]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def classify_node(state: NewsTriageState, config: RunnableConfig) -> NewsTriageState:
    """Haiku batch-classifies all headlines (capped at MAX_HEADLINES_PER_BATCH most-recent)."""
    llm: LLMClient = config["configurable"]["llm"]
    session: Session = config["configurable"]["session"]

    system = load_prompt("news_classify_v1.txt")
    capped = sorted(
        state["raws"], key=lambda r: r.published_at, reverse=True
    )[:MAX_HEADLINES_PER_BATCH]
    user = json.dumps({
        "ticker": state["ticker"],
        "headlines": [
            {"url_hash": r.url_hash, "headline": r.headline, "snippet": r.snippet}
            for r in capped
        ],
    })
    result, tel = llm_node_call(
        purpose="news_classify",
        model=config["configurable"].get("classify_model", HAIKU),
        system=system,
        user=user,
        schema=NewsTriageBatch,
        fallback_factory=lambda: NewsTriageBatch(items=[]),
        llm=llm,
        session=session,
        max_tokens=4096,
    )
    return {**state, "classifications": result.items, "telemetry": {**state["telemetry"], **tel}}


def critic_node(state: NewsTriageState, config: RunnableConfig) -> NewsTriageState:
    """Haiku reviews classifier output for problems. Returns url_hashes of flagged items."""
    llm: LLMClient = config["configurable"]["llm"]
    session: Session = config["configurable"]["session"]

    system = load_prompt("news_critic_v1.txt")
    raw_by_hash = {r.url_hash: r for r in state["raws"]}
    user = json.dumps({
        "ticker": state["ticker"],
        "pairs": [
            {
                "url_hash": cls.url_hash,
                "headline": (
                    raw_by_hash[cls.url_hash].headline if cls.url_hash in raw_by_hash else ""
                ),
                "snippet": (
                    raw_by_hash[cls.url_hash].snippet[:400] if cls.url_hash in raw_by_hash else ""
                ),
                "classifier_output": cls.model_dump(),
            }
            for cls in state["classifications"]
        ],
    })
    result, tel = llm_node_call(
        purpose="news_critic",
        model=config["configurable"].get("critic_model", SONNET),
        system=system,
        user=user,
        schema=NewsCriticReview,
        fallback_factory=lambda: NewsCriticReview(flagged=[]),
        llm=llm,
        session=session,
        max_tokens=2048,
    )
    return {**state, "flagged": result.flagged, "telemetry": {**state["telemetry"], **tel}}


def arbitrate_node(state: NewsTriageState, config: RunnableConfig) -> NewsTriageState:
    """Sonnet re-evaluates only the items flagged by the critic."""
    llm: LLMClient = config["configurable"]["llm"]
    session: Session = config["configurable"]["session"]

    raw_by_hash = {r.url_hash: r for r in state["raws"]}
    cls_by_hash = {c.url_hash: c for c in state["classifications"]}

    system = load_prompt("news_arbitrate_v1.txt")
    user = json.dumps({
        "ticker": state["ticker"],
        "items": [
            {
                "url_hash": h,
                "headline": raw_by_hash[h].headline if h in raw_by_hash else "",
                "snippet": (raw_by_hash[h].snippet[:400] if h in raw_by_hash else ""),
                "previous_classification": cls_by_hash[h].model_dump() if h in cls_by_hash else {},
            }
            for h in state["flagged"]
        ],
    })
    result, tel = llm_node_call(
        purpose="news_arbitrate",
        model=config["configurable"].get("arbitrate_model", SONNET),
        system=system,
        user=user,
        schema=NewsTriageBatch,
        fallback_factory=lambda: NewsTriageBatch(items=[]),
        llm=llm,
        session=session,
        max_tokens=4096,
    )
    revised_by_hash = {r.url_hash: r for r in result.items}
    final = [revised_by_hash.get(c.url_hash, c) for c in state["classifications"]]
    return {**state, "final": final, "telemetry": {**state["telemetry"], **tel}}


def copy_to_final(state: NewsTriageState) -> NewsTriageState:
    """No-op node: copies classifications to final when no arbitration needed."""
    return {**state, "final": list(state["classifications"])}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_after_critic(state: NewsTriageState) -> Literal["arbitrate", "no_arbitrate"]:
    """Route to arbitrate if the critic flagged any items; otherwise skip to no_arbitrate."""
    return "arbitrate" if state["flagged"] else "no_arbitrate"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_news_triage_graph() -> object:
    """Build and compile the news triage graph. Call once at app startup.

    ``llm`` and ``session`` are passed at invoke time via
    ``config["configurable"]``, not at compile time.

    Example invoke::

        result = graph.invoke(
            initial_state,
            config={
                "configurable": {
                    "thread_id": f"news-{ticker}-{date.today()}",
                    "llm": llm,
                    "session": session,
                }
            },
        )
    """
    g: StateGraph = StateGraph(NewsTriageState)
    g.add_node("classify", classify_node)
    g.add_node("critic", critic_node)
    g.add_node("arbitrate", arbitrate_node)
    g.add_node("no_arbitrate", copy_to_final)
    g.add_edge(START, "classify")
    g.add_edge("classify", "critic")
    g.add_conditional_edges(
        "critic",
        route_after_critic,
        {"arbitrate": "arbitrate", "no_arbitrate": "no_arbitrate"},
    )
    g.add_edge("arbitrate", END)
    g.add_edge("no_arbitrate", END)
    return g.compile(checkpointer=make_checkpointer())
