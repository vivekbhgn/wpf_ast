import argparse
import logging
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

# ──  Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# Cache file is set per-component in main()
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
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Anthropic model for micro-summaries")
    parser.add_argument("--prd-model", default="claude-sonnet-4-5", help="Anthropic model for final PRD generation (Sonnet recommended)")
    parser.add_argument("--screenshot", default="", help="Optional path to a screenshot of the running WPF component for visual reference")
    parser.add_argument("--clear-cache", action="store_true", help="Delete cached summaries and regenerate from scratch")
    args = parser.parse_args()

    # Per-component cache file to avoid cross-contamination
    global CACHE_FILE
    CACHE_FILE = f"summaries_cache_{args.component}.json"
    log.info("Cache file: %s", CACHE_FILE)

    if args.clear_cache and Path(CACHE_FILE).exists():
        Path(CACHE_FILE).unlink()
        log.info("Cleared cache file: %s", CACHE_FILE)

    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY environment variable not set. Please set it in .env")
        print("Error: ANTHROPIC_API_KEY environment variable not set. Please set it in .env")
        return

    log.info("Starting selective scan for component '%s' in project: %s", args.component, args.project)
    print(f"Scanning {args.project} selectively for component '{args.component}' ...")
    graph = WpfAstGraph.from_selective_scan(args.project, args.component)
    agent_tools.init_tools(graph)
    log.info("Graph built. Nodes=%d, Edges=%d", graph.G.number_of_nodes(), graph.G.number_of_edges())

    # Initialize LLMs — fast model for micro-summaries, powerful model for final PRD
    log.info("Initializing summarizer LLM: %s", args.model)
    llm_fast = ChatAnthropic(model=args.model, temperature=0.0)
    log.info("Initializing PRD generator LLM: %s", args.prd_model)
    llm_prd = ChatAnthropic(model=args.prd_model, temperature=0.0)

    log.info("Step 1: Finding related components for '%s' (depth=1)...", args.component)
    print(f"\n1. Finding related components for {args.component}...")
    try:
        related_json = agent_tools.get_related_components.invoke({
            "name": args.component,
            "depth": 2,
            "relation_types": ""
        })
        related_data = json.loads(related_json)
    except Exception as e:
        print(f"Error getting related components: {e}")
        return

    nodes = related_data.get("nodes", {})
    if not nodes:
        log.warning("No related nodes found for '%s'. Component may not be in graph.", args.component)
        print("No related nodes found or component does not exist.")
        return

    log.info("Found %d related components.", len(nodes))
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
    log.info("Step 2: Summarising %d components. Throttle=3s per API call.", len(nodes))
    for nid, node_info in nodes.items():
        node_name = node_info.get("label", nid)
        
        if nid in cache:
            log.debug("  [cache hit] %s", node_name)
            print(f"  [Cached] {node_name}")
            summaries.append(f"{node_name}: {cache[nid]}")
            continue

        log.info("  [API call] Summarising '%s'...", node_name)
        print(f"  [API Call] Summarizing {node_name}...")
        try:
            local_meta = agent_tools.summarize_component.invoke({"name": node_name})
            prompt = (
                f"You are a technical analyst. Briefly summarize the role of the WPF component '{node_name}' "
                f"in 2-3 sentences based on this metadata:\n\n{local_meta}"
            )
            response = llm_fast.invoke([HumanMessage(content=prompt)])
            summary_text = str(response.content)
            log.debug("  [API response] %s → %d chars", node_name, len(summary_text))
            
            cache[nid] = summary_text
            summaries.append(f"{node_name}: {summary_text}")
            time.sleep(3)
        except Exception as e:
            log.error("  Failed to summarise '%s': %s", node_name, e)
            print(f"  [Error] Failed to summarize {node_name}: {e}")

    # Save cache once after all summaries are generated
    save_cache(cache)
    log.info("Saved %d summaries to cache.", len(cache))

    log.info("Step 3: Assembling final PRD prompt...")
    print("\n3. Generating final Product Requirements Document (PRD)...")

    main_meta = agent_tools.summarize_component.invoke({"name": args.component})
    log.debug("Main component metadata: %d chars", len(main_meta))
    if main_viewmodel_meta:
        main_meta += "\n\n=== ASSOCIATED VIEWMODEL ===\n" + main_viewmodel_meta
        log.debug("Appended ViewModel metadata. Total main_meta: %d chars", len(main_meta))

    deps_text = "\n".join(f"- {s}" for s in summaries)
    log.debug("Total dependency summaries: %d items, %d chars total", len(summaries), len(deps_text))

    # Build the structured binding map for XAML components
    binding_map = ""
    try:
        binding_map = agent_tools.extract_binding_map.invoke({"component_name": args.component})
        log.info("Binding map extracted: %d chars", len(binding_map))
    except Exception as e:
        log.warning("Could not extract binding map: %s", e)

    # Load screenshot reference if provided
    screenshot_note = ""
    if args.screenshot and Path(args.screenshot).exists():
        screenshot_note = (
            f"\n\nIMPORTANT: A screenshot of the running WPF component is available at: {args.screenshot}\n"
            f"The React implementation must match this visual layout as closely as possible.\n"
        )
        # Convert to base64 for Anthropic vision API if it's an image
        import base64
        with open(args.screenshot, 'rb') as img_f:
            img_data = base64.b64encode(img_f.read()).decode('utf-8')
        ext = Path(args.screenshot).suffix.lower().lstrip('.')
        media_type = f"image/{ext}" if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp') else f"image/png"
        log.info("Screenshot loaded: %s (%s)", args.screenshot, media_type)

    final_prompt = (
        f"You are an expert software architect. I am migrating a WPF component named '{args.component}' to a modern React.js + TypeScript tech stack.\n\n"
        f"Here is the COMPLETE graph structure and metadata of the primary component(s) (including its ViewModel if applicable).\n"
        f"NOTE: The raw source code of the XAML and ViewModel is included below — use it to understand the EXACT layout, data bindings, business logic, and validation rules.\n"
        f"{main_meta}\n\n"
        f"Here is the structured data-binding map showing every UI-to-data connection:\n"
        f"{binding_map}\n\n"
        f"Here are the summaries of the other dependencies and child controls it interacts with:\n"
        f"{deps_text}\n\n"
        f"{screenshot_note}"
        f"Based on ALL of this, create a comprehensive Product Requirements Document (PRD) that another developer or AI agent can use to EXACTLY recreate this component in React.js + TypeScript. "
        f"The PRD MUST include:\n"
        f"1. **Data Models & TypeScript Interfaces** — every entity, enum, and their exact fields/values\n"
        f"2. **Component Hierarchy** — exact tree structure mapping WPF controls to React components\n"
        f"3. **State Management** — Redux/Zustand store shape mirroring ViewModel properties\n"
        f"4. **Data Binding Table** — every UI element ↔ state property connection with mode (one-way/two-way)\n"
        f"5. **Event Handlers & Commands** — button clicks, selection changes, keyboard shortcuts\n"
        f"6. **Business Logic** — validation rules, computed properties, conditional visibility\n"
        f"7. **API Contract** — expected REST endpoints matching the service layer\n"
        f"8. **Exact Layout Spec** — grid columns, row definitions, sizing, margins from the XAML\n"
    )
    log.info("Final PRD prompt assembled: %d chars total", len(final_prompt))

    try:
        print("  Sending final prompt to Claude Sonnet for PRD generation...")
        # Use vision API if screenshot is provided
        if args.screenshot and Path(args.screenshot).exists():
            messages = [HumanMessage(content=[
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                {"type": "text", "text": final_prompt}
            ])]
        else:
            messages = [HumanMessage(content=final_prompt)]
        
        final_response = llm_prd.invoke(messages)
        doc_content = str(final_response.content)
        
        doc_filename = f"{args.component}_PRD.md"
        with open(doc_filename, "w", encoding="utf-8") as f:
            f.write(doc_content)
        
        log.info("PRD saved to %s (%d chars)", doc_filename, len(doc_content))
        print(f"\nSuccess! PRD generated and saved to {doc_filename}")
        
    except Exception as e:
        log.error("Failed to generate PRD: %s", e)
        print(f"Error generating final PRD: {e}")

if __name__ == "__main__":
    main()
