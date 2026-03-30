import argparse
import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from graph_builder import WpfAstGraph
import agent_tools

load_dotenv()

CACHE_FILE = "summaries_cache.json"

def load_cache():
    if Path(CACHE_FILE).exists():
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)

def main():
    parser = argparse.ArgumentParser(description="WPF to PRD generator (Iterative Approach)")
    parser.add_argument("--project", required=True, help="Path to WPF project root")
    parser.add_argument("--component", required=True, help="Target component (e.g. FlowView)")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Anthropic model")
    args = parser.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set. Please set it in .env")
        return

    print(f"Scanning {args.project} selectively for component '{args.component}' ...")
    graph = WpfAstGraph.from_selective_scan(args.project, args.component)
    agent_tools.init_tools(graph)

    # Initialize LLM
    llm = ChatAnthropic(model=args.model, temperature=0.0)

    print(f"\n1. Finding related components for {args.component}...")
    try:
        related_json = agent_tools.get_related_components.invoke({
            "name": args.component,
            "depth": 1,
            "relation_types": ""
        })
        related_data = json.loads(related_json)
    except Exception as e:
        print(f"Error getting related components: {e}")
        return

    nodes = related_data.get("nodes", {})
    if not nodes:
        print("No related nodes found or component does not exist.")
        return

    print(f"Found {len(nodes)} related components.")

    cache = load_cache()
    summaries = []
    
    # 1.5 Find the main ViewModel (if we're analyzing a View)
    main_viewmodel_nid = None
    main_viewmodel_meta = ""
    
    # Heuristic 1: Global search by naming convention (FlowView -> FlowViewModel)
    base_name = args.component.replace("View", "")
    guess_vm_name = base_name + "ViewModel"
    vm_nid = graph.find_node(guess_vm_name)
    
    if vm_nid:
        main_viewmodel_nid = vm_nid
        label = graph.G.nodes[vm_nid].get("label", guess_vm_name)
        print(f"\n[+] Auto-detected primary ViewModel via naming convention: {label}")
        try:
            main_viewmodel_meta = agent_tools.summarize_component.invoke({"name": label})
            vm_related = json.loads(agent_tools.get_related_components.invoke({"name": label, "depth": 1, "relation_types": ""}))
            vm_nodes = vm_related.get("nodes", {})
            nodes.update(vm_nodes)
            print(f"  [+] Merged {len(vm_nodes)} ViewModel dependencies into the summary queue.")
        except Exception as e:
            print(f"Error summarizing ViewModel: {e}")
    else:
        # Heuristic 2: Scan related nodes
        for nid, node_info in nodes.items():
            kind = node_info.get("kind", "")
            label = node_info.get("label", nid)
            if "ViewModel" in label and base_name in label:
                main_viewmodel_nid = nid
                print(f"\n[+] Auto-detected primary ViewModel via graph relations: {label}")
                try:
                    main_viewmodel_meta = agent_tools.summarize_component.invoke({"name": label})
                    vm_related = json.loads(agent_tools.get_related_components.invoke({"name": label, "depth": 1, "relation_types": ""}))
                    vm_nodes = vm_related.get("nodes", {})
                    nodes.update(vm_nodes)
                    print(f"  [+] Merged {len(vm_nodes)} ViewModel dependencies into the summary queue.")
                except Exception as e:
                    print(f"Error summarizing ViewModel: {e}")
                break

    print("\n2. Generating micro-summaries step-by-step (throttled)...")
    for nid, node_info in nodes.items():
        node_name = node_info.get("label", nid)
        
        if nid in cache:
            print(f"  [Cached] {node_name}")
            summaries.append(f"{node_name}: {cache[nid]}")
            continue

        print(f"  [API Call] Summarizing {node_name}...")
        try:
            local_meta = agent_tools.summarize_component.invoke({"name": node_name})
            prompt = (
                f"You are a technical analyst. Briefly summarize the role of the WPF component '{node_name}' "
                f"in 2-3 sentences based on this metadata:\n\n{local_meta}"
            )
            response = llm.invoke([HumanMessage(content=prompt)])
            summary_text = str(response.content)
            
            cache[nid] = summary_text
            summaries.append(f"{node_name}: {summary_text}")
            save_cache(cache)
            time.sleep(3)
        except Exception as e:
            print(f"  [Error] Failed to summarize {node_name}: {e}")

    print("\n3. Generating final Product Requirements Document (PRD)...")
    
    main_meta = agent_tools.summarize_component.invoke({"name": args.component})
    if main_viewmodel_meta:
        main_meta += "\n\n=== ASSOCIATED VIEWMODEL ===\n" + main_viewmodel_meta
        
    deps_text = "\n".join(f"- {s}" for s in summaries)

    final_prompt = (
        f"You are an expert software architect. I am migrating a WPF component named '{args.component}' to a modern tech stack (e.g. React/Node).\n\n"
        f"Here is the local graph structure and metadata of the primary component(s) (including its ViewModel if applicable):\n"
        f"{main_meta}\n\n"
        f"Here are the summaries of the other dependencies and child controls it interacts with:\n"
        f"{deps_text}\n\n"
        f"Based on this, create a comprehensive Product Requirements Document (PRD) that another developer or AI agent can use to fully recreate this component in any tech stack. "
        f"Focus on the data it needs, the UI elements it requires, its dependencies, state management requirements, and business logic."
    )

    try:
        print("  Sending final prompt to Claude...")
        final_response = llm.invoke([HumanMessage(content=final_prompt)])
        doc_content = str(final_response.content)
        
        doc_filename = f"{args.component}_PRD.md"
        with open(doc_filename, "w", encoding="utf-8") as f:
            f.write(doc_content)
        
        print(f"\nSuccess! PRD generated and saved to {doc_filename}")
        
    except Exception as e:
        print(f"Error generating final PRD: {e}")

if __name__ == "__main__":
    main()
