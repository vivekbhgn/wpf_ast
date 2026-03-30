"""
agent.py
--------
A LangGraph ReAct agent wired up to the WPF AST graph tools.
The agent can answer questions like:
  - "What components are related to CustomerViewModel?"
  - "If I change IOrderService, what breaks?"
  - "What XAML controls bind to the SelectedCustomer property?"
  - "Show me the full inheritance chain for BaseViewModel."
  - "Which methods call SaveAsync?"
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from graph_builder import WpfAstGraph
import agent_tools as tools_module
from agent_tools import (
    find_component,
    get_related_components,
    get_direct_dependencies,
    get_dependents,
    get_call_chain,
    get_xaml_bindings,
    get_inheritance_chain,
    find_by_attribute,
    summarize_component,
    search_components,
    get_impact_analysis,
    export_subgraph_dot,
    get_graph_stats,
)

load_dotenv()


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an expert WPF application analyst with deep knowledge of C#, MVVM,
and WPF XAML patterns. You have access to a pre-built AST graph of the entire
WPF codebase. The graph captures:

  • Every class, interface, struct, enum, method, property, field, and event
  • Every XAML view (Window, UserControl, Page) and its named controls
  • Data bindings, ICommand bindings, event handlers
  • Inheritance chains (inherits / implements)
  • Constructor dependency injection
  • Method call chains
  • Cross-references between XAML views and ViewModels

When answering questions:
1. Start by finding the component with find_component or search_components.
2. Use get_related_components to discover neighbors.
3. Use more specific tools for targeted questions (call chains, bindings, impact).
4. Always cite node IDs and file paths in your answer.
5. When asked about migration impact, use get_impact_analysis.
6. For visualizing relationships, offer to use export_subgraph_dot.

Relationship types available:
  inherits, implements, contains, calls, instantiates, depends_on,
  binds_to, commands, handles_event, data_context, navigates_to,
  uses_resource, references, overrides, part_of_xaml
"""


# ── Agent factory ──────────────────────────────────────────────────────────────

ALL_TOOLS = [
    find_component,
    get_related_components,
    get_direct_dependencies,
    get_dependents,
    get_call_chain,
    get_xaml_bindings,
    get_inheritance_chain,
    find_by_attribute,
    summarize_component,
    search_components,
    get_impact_analysis,
    export_subgraph_dot,
    get_graph_stats,
]


def build_agent(graph: WpfAstGraph, model: str = "claude-haiku-4-5-20251001") -> Any:
    """
    Build a LangGraph ReAct agent with the WPF AST tools bound to the graph.

    Args:
        graph:  Pre-built WpfAstGraph instance.
        model:  Anthropic model name.

    Returns:
        A compiled LangGraph agent (invoke / stream compatible).
    """
    tools_module.init_tools(graph)

    llm   = ChatAnthropic(model=model, temperature=0.0, streaming=True)
    agent = create_react_agent(llm, tools=ALL_TOOLS, prompt=SYSTEM_PROMPT)
    return agent


def ask(agent: Any, question: str, verbose: bool = True) -> str:
    """
    Send a question to the agent and return the final answer.

    Args:
        agent:    The compiled LangGraph agent.
        question: Natural language question about the codebase.
        verbose:  Print intermediate steps if True.
    """
    result = agent.invoke({"messages": [HumanMessage(content=question)]})
    messages = result.get("messages", [])

    if verbose:
        for msg in messages[:-1]:
            kind = type(msg).__name__
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    print(f"  [tool] {tc['name']}({list(tc['args'].keys())})")
            elif kind == 'ToolMessage':
                snippet = str(msg.content)[:120].replace('\n', ' ')
                print(f"  [result] {snippet}...")

    # Return last AI message
    for msg in reversed(messages):
        if hasattr(msg, 'content') and msg.content and not hasattr(msg, 'tool_calls'):
            return msg.content
        if hasattr(msg, 'content') and msg.content and not msg.tool_calls:
            return msg.content
    return str(messages[-1].content) if messages else ""


# ── CLI / demo ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="WPF AST graph agent")
    parser.add_argument("--project", required=True, help="Path to WPF project root")
    parser.add_argument("--question", "-q", default=None,
                        help="Question to ask (interactive mode if omitted)")
    parser.add_argument("--export-graph", default=None,
                        help="Export graph to .json or .graphml")
    parser.add_argument("--stats", action="store_true",
                        help="Print graph statistics and exit")
    args = parser.parse_args()

    # Build graph
    print(f"\nScanning {args.project} ...")
    graph = WpfAstGraph.from_directory(args.project)

    if args.export_graph:
        graph.save(args.export_graph)

    if args.stats:
        from pprint import pprint
        pprint(graph.stats())
        sys.exit(0)

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("[warn] ANTHROPIC_API_KEY not set — running in tool-demo mode only")
        tools_module.init_tools(graph)
        target = (args.question or "CustomerViewModel").split()[-1]
        nid    = graph.find_node(target)
        if nid:
            print(f"\nFound node: {nid}")
            result = graph.get_related(nid, depth=2)
            print(f"Related nodes ({len(result['nodes'])}):")
            for n, a in list(result['nodes'].items())[:10]:
                print(f"  {a.get('kind','?'):20} {a.get('label', n)}")
        sys.exit(0)

    agent = build_agent(graph)

    if args.question:
        print(f"\nQ: {args.question}")
        answer = ask(agent, args.question)
        print(f"\nA: {answer}")
    else:
        # Interactive mode
        print("\nWPF AST Agent — type your question (Ctrl+C to exit)\n")
        while True:
            try:
                q = input("Q: ").strip()
                if not q:
                    continue
                answer = ask(agent, q)
                print(f"A: {answer}\n")
            except KeyboardInterrupt:
                print("\nBye!")
                break
