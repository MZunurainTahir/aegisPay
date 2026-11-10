"""
agent/graph.py
--------------
LangGraph-based implementation of the Aegis AI CFO agent.

Architecture:
  [User Goal]
       │
       ▼
  [Plan / Reason] ──(Select Gateway Tool)──► [Execute via Aegis Gateway]
       ▲                                               │
       │                                               ▼
  [Loop Condition] ◄──(Evaluate Gateway Response)──────┘
       │ (Done or max turns)
       ▼
  [Synthesize Final CFO Summary]

CRITICAL PRINCIPLE:
Even within LangGraph, the agent never talks to PayPal directly. Every money-moving
action is dispatched to the Aegis Gateway REST endpoint (/act), which executes the
mandate policy, scores ML risk, and writes the immutable audit row.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated, Any, Dict, List, TypedDict

import httpx
from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

load_dotenv()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from agent.agent import SYSTEM_PROMPT, TOOLS, _run_tool, _sanitize_invoice_data
from agent.llm import chat

logger = logging.getLogger("aegis.graph")
GW = os.environ.get("GATEWAY_URL", "http://localhost:8000")


class AgentState(TypedDict):
    goal: str
    messages: List[Dict[str, Any]]
    iterations: int
    actions_taken: List[Dict[str, Any]]
    final_output: str
    complete: bool


def reason_node(state: AgentState) -> AgentState:
    """Agent reasons about the goal and selects a tool or produces a final response."""
    messages = state["messages"]
    content, tool_calls = chat(messages, TOOLS)

    new_messages = list(messages)
    actions = list(state.get("actions_taken", []))

    if not tool_calls:
        new_messages.append({"role": "assistant", "content": content or ""})
        return {
            **state,
            "messages": new_messages,
            "final_output": content or "Goal fulfilled.",
            "complete": True,
            "iterations": state["iterations"] + 1,
        }

    # Add assistant message with tool calls
    new_messages.append({
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"]),
                },
            }
            for tc in tool_calls
        ],
    })

    # Dispatch tool calls through Aegis Gateway
    for tc in tool_calls:
        logger.info("LangGraph tool execution: %s", tc["name"])
        raw_res = _run_tool(tc["name"], tc["arguments"])
        actions.append({"tool": tc["name"], "arguments": tc["arguments"], "result": raw_res})

        if tc["name"] == "list_invoices":
            serialized = _sanitize_invoice_data(raw_res)
        else:
            serialized = json.dumps(raw_res)

        new_messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": serialized,
        })

    return {
        **state,
        "messages": new_messages,
        "actions_taken": actions,
        "complete": False,
        "iterations": state["iterations"] + 1,
    }


def should_continue(state: AgentState) -> str:
    """Routing condition: finish if complete or if max iterations reached."""
    if state.get("complete", False):
        return "end"
    if state.get("iterations", 0) >= 8:
        return "end"
    return "reason"


def create_aegis_graph():
    """Build and compile the LangGraph agent state graph."""
    workflow = StateGraph(AgentState)

    workflow.add_node("reason", reason_node)
    workflow.set_entry_point("reason")

    workflow.add_conditional_edges(
        "reason",
        should_continue,
        {
            "reason": "reason",
            "end": END,
        },
    )

    return workflow.compile()


def run_graph(goal: str) -> str:
    """Execute the LangGraph agent on a given business goal."""
    graph = create_aegis_graph()
    initial_state: AgentState = {
        "goal": goal,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": goal},
        ],
        "iterations": 0,
        "actions_taken": [],
        "final_output": "",
        "complete": False,
    }

    result = graph.invoke(initial_state)
    return result.get("final_output", "(No response generated)")


if __name__ == "__main__":
    test_goal = sys.argv[1] if len(sys.argv) > 1 else "Chase all overdue invoices"
    print(f"Running LangGraph agent with goal: {test_goal}")
    print(run_graph(test_goal))

