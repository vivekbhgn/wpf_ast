"""
agent_tools.py
--------------
LangChain @tool functions that expose the WPF AST graph to an AI agent.

The agent can:
  1. find_component           — locate a node by name
  2. get_related_components   — BFS neighbors up to N hops
  3. get_direct_dependencies  — what this class directly depends on
  4. get_dependents           — what depends on this class
  5. get_call_chain           — methods that call into this class
  6. get_xaml_bindings        — XAML controls bound to this VM
  7. get_inheritance_chain    — full ancestor + descendant tree
  8. find_by_attribute        — find classes by C# attribute (e.g. [RelayCommand])
  9. summarize_component      — rich text summary of a single node
  10. search_components       — fuzzy name search across all nodes
  11. get_impact_analysis     — which components break if this one changes
  12. export_subgraph_dot     — render DOT source for visualization
"""

from __future__ import annotations
import json
import textwrap
from typing import Optional

from langchain_core.tools import tool

# The graph is injected at import time — call init_tools(graph) first.
_GRAPH = None

def init_tools(graph) -> None:
    """Call this with a WpfAstGraph instance before using the tools."""
    global _GRAPH
    _GRAPH = graph


def _require_graph():
    if _GRAPH is None:
        raise RuntimeError("Call init_tools(graph) before using agent tools.")
    return _GRAPH


# ── 1. find_component ─────────────────────────────────────────────────────────

@tool
def find_component(name: str) -> str:
    """
    Find a WPF component (class, ViewModel, XAML view, method, property) by name.
    Returns a JSON summary of the node and its immediate relationships.

    Args:
        name: Component name to find. Can be a class name (CustomerViewModel),
              a method name (SaveAsync), a XAML view (CustomerFormView),
              or a full namespace-qualified name.
    """
    g = _require_graph()
    nid = g.find_node(name)
    if not nid:
        # Fuzzy fallback
        candidates = [n for n in g.G.nodes()
                      if name.lower() in g.G.nodes[n].get('label', '').lower()]
        if candidates:
            nid = candidates[0]
        else:
            return json.dumps({"error": f"Component '{name}' not found in graph.",
                               "suggestion": "Try search_components(name) for partial matches."})

    attrs = dict(g.G.nodes[nid])
    out_edges = [(tgt, d.get('rel')) for _, tgt, d in g.G.out_edges(nid, data=True)]
    in_edges  = [(src, d.get('rel')) for src, _, d in g.G.in_edges(nid, data=True)]

    return json.dumps({
        "node_id":       nid,
        "attributes":    attrs,
        "outgoing_rels": out_edges[:30],
        "incoming_rels": in_edges[:30],
    }, indent=2, default=str)


# ── 2. get_related_components ─────────────────────────────────────────────────

@tool
def get_related_components(name: str, depth: int = 2,
                           relation_types: str = "") -> str:
    """
    Return all components related to a given component within N hops.
    Traverses both incoming and outgoing edges.

    Args:
        name: Component name (class, method, XAML view, etc.).
        depth: How many hops to traverse (default 2, max 4).
        relation_types: Comma-separated list of edge types to follow.
            Options: inherits, implements, contains, calls, instantiates,
                     depends_on, binds_to, commands, handles_event,
                     data_context, navigates_to, references, overrides,
                     part_of_xaml.
            Leave empty to follow ALL relation types.
    """
    g    = _require_graph()
    nid  = g.find_node(name)
    if not nid:
        return json.dumps({"error": f"Component '{name}' not found."})

    depth   = min(int(depth), 4)
    filters = [r.strip() for r in relation_types.split(',') if r.strip()] or None
    result  = g.get_related(nid, depth=depth, rel_filter=filters)

    # Compact the output for the agent
    nodes_summary = {
        nid: {
            'label': attrs.get('label', nid),
            'kind':  attrs.get('kind', '?'),
            'file':  attrs.get('file', ''),
        }
        for nid, attrs in result['nodes'].items()
    }
    return json.dumps({
        "center":       nid,
        "depth":        depth,
        "relation_filter": filters,
        "total_related": len(result['nodes']),
        "nodes":        nodes_summary,
        "edges":        result['edges'],
    }, indent=2, default=str)


# ── 3. get_direct_dependencies ────────────────────────────────────────────────

@tool
def get_direct_dependencies(class_name: str) -> str:
    """
    Return everything a class directly depends on:
    base classes, interfaces, injected services (ctor params),
    property/field type references, and instantiated types.

    Args:
        class_name: Simple or qualified class name.
    """
    g   = _require_graph()
    nid = g.find_node(class_name)
    if not nid:
        return json.dumps({"error": f"'{class_name}' not found."})

    dep_rels = {'inherits', 'implements', 'depends_on', 'references', 'instantiates'}
    deps: list[dict] = []
    for _, tgt, data in g.G.out_edges(nid, data=True):
        rel = data.get('rel', '')
        if rel in dep_rels:
            tgt_attrs = g.G.nodes.get(tgt, {})
            deps.append({
                'target':   tgt,
                'label':    tgt_attrs.get('label', tgt),
                'kind':     tgt_attrs.get('kind', '?'),
                'relation': rel,
                'detail':   {k: v for k, v in data.items() if k != 'rel'},
            })

    return json.dumps({
        "class":        class_name,
        "node_id":      nid,
        "dependencies": deps,
        "count":        len(deps),
    }, indent=2, default=str)


# ── 4. get_dependents ────────────────────────────────────────────────────────

@tool
def get_dependents(class_name: str) -> str:
    """
    Return all components that directly depend on this class
    (classes that inherit it, implement it, inject it, or reference it).

    Args:
        class_name: Simple or qualified class name.
    """
    g   = _require_graph()
    nid = g.find_node(class_name)
    if not nid:
        return json.dumps({"error": f"'{class_name}' not found."})

    dep_rels = {'inherits', 'implements', 'depends_on', 'references', 'instantiates',
                'data_context', 'commands', 'binds_to'}
    dependents: list[dict] = []
    for src, _, data in g.G.in_edges(nid, data=True):
        rel = data.get('rel', '')
        if rel in dep_rels:
            src_attrs = g.G.nodes.get(src, {})
            dependents.append({
                'source':   src,
                'label':    src_attrs.get('label', src),
                'kind':     src_attrs.get('kind', '?'),
                'file':     src_attrs.get('file', ''),
                'relation': rel,
            })

    return json.dumps({
        "class":      class_name,
        "node_id":    nid,
        "dependents": dependents,
        "count":      len(dependents),
    }, indent=2, default=str)


# ── 5. get_call_chain ────────────────────────────────────────────────────────

@tool
def get_call_chain(method_name: str, direction: str = "both") -> str:
    """
    Trace the call chain for a method: who calls it and what it calls.

    Args:
        method_name: Method name (e.g. 'SaveAsync', 'LoadOrdersAsync').
        direction: 'callers' (who calls this), 'callees' (what this calls),
                   or 'both' (default).
    """
    g = _require_graph()
    method_ids = g._method_by_name.get(method_name, [])
    if not method_ids:
        nid = g.find_node(method_name)
        method_ids = [nid] if nid else []
    if not method_ids:
        return json.dumps({"error": f"Method '{method_name}' not found."})

    result: list[dict] = []
    for mid in method_ids:
        callers, callees = [], []
        if direction in ('callers', 'both'):
            for src, _, d in g.G.in_edges(mid, data=True):
                if d.get('rel') == 'calls':
                    attrs = g.G.nodes.get(src, {})
                    callers.append({'id': src, 'label': attrs.get('label', src),
                                    'kind': attrs.get('kind'), 'file': attrs.get('file')})
        if direction in ('callees', 'both'):
            for _, tgt, d in g.G.out_edges(mid, data=True):
                if d.get('rel') == 'calls':
                    attrs = g.G.nodes.get(tgt, {})
                    callees.append({'id': tgt, 'label': attrs.get('label', tgt),
                                    'kind': attrs.get('kind'), 'file': attrs.get('file')})
        result.append({'method_id': mid, 'callers': callers, 'callees': callees})

    return json.dumps(result, indent=2, default=str)


# ── 6. get_xaml_bindings ─────────────────────────────────────────────────────

@tool
def get_xaml_bindings(viewmodel_or_view_name: str) -> str:
    """
    For a ViewModel or XAML view, return all data bindings:
    which XAML controls bind to which ViewModel properties/commands,
    and which event handlers are wired up.

    Args:
        viewmodel_or_view_name: E.g. 'CustomerViewModel' or 'CustomerFormView'.
    """
    g    = _require_graph()
    nid  = g.find_node(viewmodel_or_view_name)
    if not nid:
        return json.dumps({"error": f"'{viewmodel_or_view_name}' not found."})

    # Find associated XAML file(s)
    xaml_ids = set()
    for src, _, d in g.G.in_edges(nid, data=True):
        if d.get('rel') in ('data_context', 'part_of_xaml'):
            xaml_ids.add(src)
    for _, tgt, d in g.G.out_edges(nid, data=True):
        if d.get('rel') in ('data_context', 'part_of_xaml'):
            xaml_ids.add(tgt)

    # Also check directly if it's already a XAML node
    if g.G.nodes[nid].get('kind', '').startswith('xaml'):
        xaml_ids.add(nid)

    bindings:  list[dict] = []
    commands:  list[dict] = []
    handlers:  list[dict] = []

    for xid in xaml_ids:
        xf = next((f for f in g._xaml_files
                   if g._xaml_by_class.get(f.x_class) == xid or
                      g._xaml_by_file.get(f.path) == xid), None)
        if not xf:
            continue
        for b in xf.all_bindings:
            bindings.append({
                'target_prop':  b.target_property,
                'source_path':  b.source_path,
                'mode':         b.mode,
                'converter':    b.converter,
            })
        for elem, cmd_path in xf.all_commands:
            commands.append({'element': elem, 'command_path': cmd_path})
        for elem, event, handler in xf.all_event_handlers:
            handlers.append({'element': elem, 'event': event, 'handler': handler})

    return json.dumps({
        "component":    viewmodel_or_view_name,
        "xaml_views":  list(xaml_ids),
        "bindings":    bindings,
        "commands":    commands,
        "handlers":    handlers,
    }, indent=2, default=str)


# ── 7. get_inheritance_chain ─────────────────────────────────────────────────

@tool
def get_inheritance_chain(class_name: str) -> str:
    """
    Return the full inheritance chain for a class:
    all ancestors (base classes + interfaces) and all descendants.

    Args:
        class_name: Class or interface name.
    """
    g   = _require_graph()
    nid = g.find_node(class_name)
    if not nid:
        return json.dumps({"error": f"'{class_name}' not found."})

    # Ancestors: follow 'inherits' and 'implements' upward
    ancestors: list[dict] = []
    q = [nid]
    visited = {nid}
    while q:
        cur = q.pop()
        for _, tgt, d in g.G.out_edges(cur, data=True):
            if d.get('rel') in ('inherits', 'implements') and tgt not in visited:
                visited.add(tgt)
                attrs = g.G.nodes.get(tgt, {})
                ancestors.append({'id': tgt, 'kind': attrs.get('kind', 'class'),
                                   'label': attrs.get('label', tgt), 'rel': d['rel']})
                q.append(tgt)

    # Descendants: reverse traversal
    descendants: list[dict] = []
    q = [nid]
    visited2 = {nid}
    while q:
        cur = q.pop()
        for src, _, d in g.G.in_edges(cur, data=True):
            if d.get('rel') in ('inherits', 'implements') and src not in visited2:
                visited2.add(src)
                attrs = g.G.nodes.get(src, {})
                descendants.append({'id': src, 'kind': attrs.get('kind', 'class'),
                                     'label': attrs.get('label', src), 'rel': d['rel']})
                q.append(src)

    return json.dumps({
        "class":       class_name,
        "node_id":     nid,
        "ancestors":   ancestors,
        "descendants": descendants,
    }, indent=2, default=str)


# ── 8. find_by_attribute ─────────────────────────────────────────────────────

@tool
def find_by_attribute(attribute_name: str) -> str:
    """
    Find all classes or methods decorated with a specific C# attribute.
    Useful for finding RelayCommand, ObservableProperty, Inject, etc.

    Args:
        attribute_name: Attribute name WITHOUT brackets, e.g. 'RelayCommand',
                        'ObservableProperty', 'HttpGet', 'Authorize'.
    """
    g = _require_graph()
    matches: list[dict] = []
    for nid, attrs in g.G.nodes(data=True):
        node_attrs = attrs.get('attributes', [])
        if isinstance(node_attrs, str):
            node_attrs = node_attrs.strip("[]'\"").split(',')
        if any(attribute_name.lower() in (a or '').lower() for a in node_attrs):
            matches.append({
                'id':    nid,
                'label': attrs.get('label', nid),
                'kind':  attrs.get('kind', '?'),
                'file':  attrs.get('file', ''),
                'line':  attrs.get('line', 0),
            })
    return json.dumps({
        "attribute":    attribute_name,
        "matches":      matches,
        "count":        len(matches),
    }, indent=2, default=str)


# ── 9. summarize_component ───────────────────────────────────────────────────

@tool
def summarize_component(name: str) -> str:
    """
    Produce a rich human-readable summary of a component: its type, location,
    all members, relationships, and migration notes.

    Args:
        name: Component name.
    """
    g   = _require_graph()
    nid = g.find_node(name)
    if not nid:
        return f"Component '{name}' not found in graph."

    attrs   = g.G.nodes[nid]
    kind    = attrs.get('kind', '?')
    label   = attrs.get('label', nid)
    ns      = attrs.get('namespace', '')
    file_   = attrs.get('file', '')
    line    = attrs.get('line', 0)

    # Count members
    members = [tgt for _, tgt, d in g.G.out_edges(nid, data=True)
               if d.get('rel') == 'contains']
    out_rels = [(tgt, d.get('rel'))
                for _, tgt, d in g.G.out_edges(nid, data=True)
                if d.get('rel') != 'contains']
    in_rels  = [(src, d.get('rel'))
                for src, _, d in g.G.in_edges(nid, data=True)
                if d.get('rel') != 'contains']

    is_vm   = attrs.get('is_viewmodel', False)
    is_svc  = attrs.get('is_service', False)

    migration_hint = ""
    if is_vm:
        migration_hint = "⚛ ViewModel → Redux Toolkit slice or Zustand store"
    elif kind in ('xaml_window', 'xaml_page'):
        migration_hint = "⚛ XAML Window/Page → React Route component"
    elif kind == 'xaml_usercontrol':
        migration_hint = "⚛ UserControl → React functional component"
    elif is_svc:
        migration_hint = "🔌 Service → ASP.NET Core Web API controller + React hook"

    lines = [
        f"── {label} ({kind}) ─────────────────────",
        f"  Namespace : {ns}" if ns else "",
        f"  File      : {file_}:{line}" if file_ else "",
        f"  Base      : {attrs.get('base_class', '')}" if attrs.get('base_class') else "",
        f"  Interfaces: {', '.join(attrs.get('interfaces', []))}" if attrs.get('interfaces') else "",
        f"  Members   : {len(members)} contained nodes",
        f"  Outgoing  : {len(out_rels)} relationship(s)",
        f"  Incoming  : {len(in_rels)} relationship(s)",
        f"  Migration : {migration_hint}" if migration_hint else "",
        "",
        "  Outgoing relationships (first 15):",
        *[f"    → {tgt}  [{rel}]" for tgt, rel in out_rels[:15]],
        "",
        "  Incoming relationships (first 15):",
        *[f"    ← {src}  [{rel}]" for src, rel in in_rels[:15]],
    ]
    return '\n'.join(l for l in lines if l is not None)


# ── 10. search_components ───────────────────────────────────────────────────

@tool
def search_components(query: str, kind_filter: str = "") -> str:
    """
    Fuzzy search for components by name across the entire graph.

    Args:
        query: Partial name to search for (case-insensitive).
        kind_filter: Optional kind to filter by (e.g. 'class', 'method',
                     'property', 'xaml_window', 'xaml_usercontrol').
    """
    g = _require_graph()
    q = query.lower()
    results: list[dict] = []
    for nid, attrs in g.G.nodes(data=True):
        label = attrs.get('label', '').lower()
        kind  = attrs.get('kind', '')
        if q not in label and q not in nid.lower():
            continue
        if kind_filter and kind != kind_filter:
            continue
        results.append({
            'id':    nid,
            'label': attrs.get('label', nid),
            'kind':  kind,
            'file':  attrs.get('file', ''),
            'line':  attrs.get('line', 0),
        })
    results.sort(key=lambda r: len(r['label']))  # shorter name = more specific match
    return json.dumps({
        "query":   query,
        "filter":  kind_filter,
        "results": results[:40],
        "total":   len(results),
    }, indent=2, default=str)


# ── 11. get_impact_analysis ──────────────────────────────────────────────────

@tool
def get_impact_analysis(class_name: str, depth: int = 3) -> str:
    """
    Determine which components would be impacted if this class changes.
    Returns a ranked list of affected components grouped by impact tier.

    Args:
        class_name: The class being changed.
        depth: How many hops of dependents to include (default 3).
    """
    g   = _require_graph()
    nid = g.find_node(class_name)
    if not nid:
        return json.dumps({"error": f"'{class_name}' not found."})

    impact_rels = {'inherits', 'implements', 'depends_on', 'references',
                   'data_context', 'commands', 'binds_to', 'calls', 'part_of_xaml'}
    tiers: dict[int, list[dict]] = {}
    visited = {nid}
    current_tier = [nid]

    for tier in range(1, depth + 1):
        next_tier = []
        for cur in current_tier:
            for src, _, d in g.G.in_edges(cur, data=True):
                if d.get('rel') in impact_rels and src not in visited:
                    visited.add(src)
                    next_tier.append(src)
                    attrs = g.G.nodes.get(src, {})
                    tiers.setdefault(tier, []).append({
                        'id':       src,
                        'label':    attrs.get('label', src),
                        'kind':     attrs.get('kind', '?'),
                        'file':     attrs.get('file', ''),
                        'relation': d.get('rel'),
                    })
        current_tier = next_tier
        if not current_tier:
            break

    total = sum(len(v) for v in tiers.values())
    return json.dumps({
        "changed_class":    class_name,
        "total_impacted":   total,
        "impact_by_tier":   tiers,
        "migration_note": (
            f"Changing {class_name} directly affects {len(tiers.get(1,[]))} components "
            f"and transitively affects {total} components total."
        ),
    }, indent=2, default=str)


# ── 12. export_subgraph_dot ──────────────────────────────────────────────────

@tool
def export_subgraph_dot(name: str, depth: int = 2) -> str:
    """
    Export a DOT-format graph string for the component and its neighbors.
    Can be rendered with Graphviz (dot -Tsvg) or pasted into https://dreampuf.github.io/GraphvizOnline/

    Args:
        name: Component name.
        depth: Neighborhood depth (default 2).
    """
    g   = _require_graph()
    nid = g.find_node(name)
    if not nid:
        return f"// Component '{name}' not found."

    result  = g.get_related(nid, depth=depth)
    G_sub   = g.G.subgraph(list(result['nodes'].keys()))

    KIND_COLORS = {
        'class':             '#B5D4F4',
        'interface':         '#C0DD97',
        'struct':            '#FAC775',
        'xaml_window':       '#F5C4B3',
        'xaml_usercontrol':  '#F5C4B3',
        'xaml_page':         '#F5C4B3',
        'xaml_control':      '#FAECE7',
        'method':            '#EEEDFE',
        'property':          '#E1F5EE',
        'field':             '#F1EFE8',
        'event':             '#FBEAF0',
        'resource_dict':     '#FAC775',
        'file':              '#D3D1C7',
    }
    REL_STYLES = {
        'inherits':     'solid,color="#185FA5"',
        'implements':   'dashed,color="#3B6D11"',
        'depends_on':   'solid,color="#993C1D"',
        'contains':     'dotted,color="#888780"',
        'calls':        'solid,color="#534AB7"',
        'instantiates': 'dashed,color="#854F0B"',
        'data_context': 'solid,color="#D85A30",penwidth=2',
        'commands':     'solid,color="#BA7517"',
        'handles_event':'dashed,color="#993556"',
        'part_of_xaml': 'solid,color="#185FA5",style=bold',
        'references':   'dotted,color="#5F5E5A"',
    }

    lines = ['digraph WpfAstGraph {',
             '  graph [fontname="Helvetica" bgcolor="#FAFAFA" rankdir=LR]',
             '  node  [fontname="Helvetica" fontsize=11 shape=box style=filled]',
             '  edge  [fontname="Helvetica" fontsize=9]', '']

    for n, attrs in G_sub.nodes(data=True):
        label = attrs.get('label', n)[:40]
        kind  = attrs.get('kind', 'class')
        color = KIND_COLORS.get(kind, '#FFFFFF')
        shape = 'ellipse' if kind.startswith('xaml') else 'box'
        border = '3' if n == nid else '1'
        lines.append(f'  "{n}" [label="{label}\\n({kind})" '
                     f'fillcolor="{color}" shape={shape} penwidth={border}]')

    lines.append('')
    for u, v, d in G_sub.edges(data=True):
        rel   = d.get('rel', '')
        style = REL_STYLES.get(rel, 'solid,color="#888780"')
        lines.append(f'  "{u}" -> "{v}" [label="{rel}" style={style}]')

    lines.append('}')
    return '\n'.join(lines)


# ── 13. get_graph_stats ──────────────────────────────────────────────────────

@tool
def get_graph_stats() -> str:
    """
    Return statistics about the entire WPF project graph:
    node counts by kind, edge counts by relation type, top connected nodes.
    """
    g     = _require_graph()
    stats = g.stats()

    # Top 10 most connected nodes
    degree = sorted(g.G.degree(), key=lambda x: x[1], reverse=True)[:10]
    top_nodes = [
        {
            'id':     nid,
            'label':  g.G.nodes[nid].get('label', nid),
            'kind':   g.G.nodes[nid].get('kind', '?'),
            'degree': deg,
        }
        for nid, deg in degree
    ]

    return json.dumps({
        **stats,
        'top_connected_nodes': top_nodes,
    }, indent=2, default=str)
