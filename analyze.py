import argparse
import sys
from graph_builder import WpfAstGraph
import agent_tools

def main():
    parser = argparse.ArgumentParser(description="WPF AST Direct Analyzer (No LLM)")
    parser.add_argument("--project", required=True, help="Path to WPF project root")
    parser.add_argument("--component", required=True, help="Component to analyze (e.g. FlowView)")
    parser.add_argument("--export-graph", default="graph_output.json", help="Path to save the generated graph (.json or .graphml)")
    parser.add_argument("--depth", type=int, default=2, help="Depth of related components to fetch")
    args = parser.parse_args()

    print(f"Scanning {args.project} selectively for component '{args.component}' ...")
    graph = WpfAstGraph.from_selective_scan(args.project, args.component)

    if args.export_graph:
        graph.save(args.export_graph)
        print(f"[OK] Graph saved locally to: {args.export_graph}")

    agent_tools.init_tools(graph)

    print(f"\n==========================================")
    print(f" Analyzing Component: {args.component}")
    print(f"==========================================\n")
    
    # 1. Component Summary
    print("--- 1. Component Summary ---\n")
    try:
        summary = agent_tools.summarize_component.invoke({"name": args.component})
        print(summary)
    except Exception as e:
        print(f"Error getting summary: {e}")

    # 2. Related Components
    print(f"\n--- 2. Related Components (Depth {args.depth}) ---\n")
    try:
        related = agent_tools.get_related_components.invoke({
            "name": args.component, 
            "depth": args.depth, 
            "relation_types": ""
        })
        print(related)
    except Exception as e:
        print(f"Error getting related components: {e}")
        
    # 3. Impact Analysis
    print(f"\n--- 3. Impact Analysis (What breaks if we change {args.component}) ---\n")
    try:
        impact = agent_tools.get_impact_analysis.invoke({
            "class_name": args.component,
            "depth": args.depth
        })
        print(impact)
    except Exception as e:
        print(f"Error getting impact analysis: {e}")

if __name__ == "__main__":
    main()
