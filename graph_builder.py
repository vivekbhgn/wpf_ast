"""
graph_builder.py
----------------
Builds a directed multi-graph (NetworkX DiGraph) from parsed C# + XAML ASTs.

Node types (stored in node attribute 'kind'):
  class | interface | struct | enum | record
  method | property | field | event
  xaml_control | xaml_window | xaml_usercontrol | xaml_page
  namespace | file | resource_dict

Edge types (stored in edge attribute 'rel'):
  inherits          class → base class
  implements        class → interface
  contains          class → method/property/field/event  (or file → class)
  calls             method → method
  instantiates      method → class
  depends_on        class → class (ctor param type)
  binds_to          xaml_control → property/class (data binding)
  commands          xaml_control → method (ICommand binding)
  handles_event     xaml_control → method (event handler in code-behind)
  data_context      xaml_root → viewmodel class
  navigates_to      xaml_control → xaml_window/page
  uses_resource     xaml_control → resource_dict
  references        class → class (field/property type reference)
  overrides         method → method (virtual override chain)
  part_of_xaml      class (code-behind) → xaml_file node
"""

from __future__ import annotations
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from parsers.csharp_parser import CSharpParser, CSharpFile, CSharpClass, CSharpMethod
from parsers.xaml_parser   import XamlParser,   XamlFile,   XamlNode


# ── Node ID helpers ────────────────────────────────────────────────────────────

def _class_id(ns: str, name: str) -> str:
    return f"{ns}.{name}" if ns else name

def _method_id(class_id: str, method_name: str, params: list = None) -> str:
    param_str = ','.join(p.type_name for p in (params or []))
    return f"{class_id}::{method_name}({param_str})"

def _xaml_id(x_class: Optional[str], path: str) -> str:
    if x_class:
        return f"xaml::{x_class}"
    return f"xaml::{Path(path).stem}"

def _ctrl_id(parent_xaml: str, node: XamlNode) -> str:
    ident = node.name or f"{node.tag}@L{node.line}"
    return f"{parent_xaml}::{ident}"


# ── Node attribute factories ───────────────────────────────────────────────────

def _class_attrs(cls: CSharpClass, file_path: str) -> dict:
    return {
        'kind':        cls.kind,
        'label':       cls.name,
        'namespace':   cls.namespace,
        'modifiers':   cls.modifiers,
        'file':        file_path,
        'line':        cls.line,
        'attributes':  cls.attributes,
        'base_class':  cls.base_class,
        'interfaces':  cls.interfaces,
        'is_viewmodel': cls.name.endswith('ViewModel') or 'INotifyPropertyChanged' in cls.interfaces,
        'is_service':  any(kw in cls.name for kw in ('Service', 'Repository', 'Context', 'Client')),
        'is_command':  'ICommand' in cls.interfaces,
        'methods':     [{'name': m.name, 'return_type': m.return_type, 'modifiers': m.modifiers, 'parameters': [p.type_name for p in m.parameters]} for m in cls.methods],
        'properties':  [{'name': p.name, 'type_name': p.type_name, 'modifiers': p.modifiers} for p in cls.properties],
        'fields':      [{'name': f.name, 'type_name': f.type_name, 'modifiers': f.modifiers} for f in cls.fields],
        'events':      [{'name': e.name, 'delegate_type': e.delegate_type} for e in cls.events]
    }

def _method_attrs(m: CSharpMethod, class_id: str) -> dict:
    return {
        'kind':        'method',
        'label':       m.name,
        'return_type': m.return_type,
        'modifiers':   m.modifiers,
        'is_async':    m.is_async,
        'is_override': m.is_override,
        'attributes':  m.attributes,
        'class_id':    class_id,
        'line':        m.line,
        'param_types': [p.type_name for p in m.parameters],
    }

def _xaml_attrs(xf: XamlFile) -> dict:
    root = xf.root_tag
    kind = ('xaml_window'     if 'Window' in root else
            'xaml_page'       if 'Page'   in root else
            'xaml_usercontrol' if 'UserControl' in root else
            'xaml_resource_dict' if 'ResourceDictionary' in root else
            'xaml_control')
    return {
        'kind':          kind,
        'label':         xf.x_class or Path(xf.path).stem,
        'file':          xf.path,
        'x_class':       xf.x_class,
        'root_tag':      xf.root_tag,
        'data_context':  xf.data_context_type,
        'named_elements': list(xf.named_elements.keys()),
    }


# ── Main builder ───────────────────────────────────────────────────────────────

class WpfAstGraph:
    """
    Builds and holds the full WPF project graph.

    Usage:
        graph = WpfAstGraph.from_directory("/path/to/MyWpfApp")
        # or incrementally:
        g = WpfAstGraph()
        g.add_csharp_file("/path/to/Foo.cs")
        g.add_xaml_file("/path/to/FooView.xaml")
        g.link_cross_references()
    """

    def __init__(self):
        self.G: nx.DiGraph = nx.DiGraph()
        # Indexes for fast lookup
        self._class_by_name:  dict[str, str] = {}   # simple_name → node_id
        self._class_by_id:    dict[str, str] = {}   # full_id → node_id (same, for clarity)
        self._xaml_by_class:  dict[str, str] = {}   # x_class → xaml node_id
        self._xaml_by_file:   dict[str, str] = {}   # file_path → xaml node_id
        self._method_by_name: dict[str, list[str]] = {}  # method_name → [node_ids]
        self._cs_files:       list[CSharpFile] = []
        self._xaml_files:     list[XamlFile]   = []

    # ── Ingestion ──────────────────────────────────────────────────────────────

    @classmethod
    def from_directory(cls, root_path: str,
                       include_patterns: list[str] = None,
                       exclude_dirs: list[str] = None) -> "WpfAstGraph":
        """
        Scan a directory recursively, parse all .cs and .xaml files,
        build the graph, and resolve cross-references.
        """
        instance = cls()
        root  = Path(root_path)
        excl  = set(exclude_dirs or ['obj', 'bin', '.git', 'node_modules', 'packages'])

        cs_files   = []
        xaml_files = []

        for f in root.rglob('*'):
            if not f.is_file():
                continue
            if any(ex in f.parts for ex in excl):
                continue
            if f.suffix.lower() == '.cs':
                cs_files.append(str(f))
            elif f.suffix.lower() == '.xaml':
                xaml_files.append(str(f))

        print(f"[WpfAstGraph] Found {len(cs_files)} .cs and {len(xaml_files)} .xaml files")

        for path in cs_files:
            try:
                instance.add_csharp_file(path)
            except Exception as e:
                print(f"  [warn] {path}: {e}")

        for path in xaml_files:
            try:
                instance.add_xaml_file(path)
            except Exception as e:
                print(f"  [warn] {path}: {e}")

        instance.link_cross_references()
        print(f"[WpfAstGraph] Graph: {instance.G.number_of_nodes()} nodes, "
              f"{instance.G.number_of_edges()} edges")
        return instance

    @classmethod
    def from_selective_scan(cls, root_path: str, component_name: str,
                            exclude_dirs: list[str] = None) -> "WpfAstGraph":
        """
        Scans only files that mention the component name, drastically saving parsing time
        for huge enterprise codebases.
        """
        instance = cls()
        root  = Path(root_path)
        excl  = set(exclude_dirs or ['obj', 'bin', '.git', 'node_modules', 'packages'])
        
        base_name = component_name.replace("View", "").replace("ViewModel", "")
        if not base_name: base_name = component_name # Fallback if empty
        
        # Build exact filename permutations
        exact_matches = {
            f"{base_name}View.xaml".lower(),
            f"{base_name}View.xaml.cs".lower(),
            f"{base_name}ViewModel.cs".lower(),
            f"{base_name}Model.cs".lower(),
            f"{base_name}.cs".lower(),
            f"{component_name}.xaml".lower(),
            f"{component_name}.xaml.cs".lower(),
            f"{component_name}.cs".lower()
        }

        target_files = []
        for f in root.rglob('*'):
            if not f.is_file() or f.suffix not in ['.cs', '.xaml']:
                continue
            if any(ex in f.parts for ex in excl):
                continue
                
            # strict exact filename matching to prevent explosion of files
            if f.name.lower() in exact_matches:
                target_files.append(f)

        print(f"[WpfAstGraph] Fast-scanned: Found {len(target_files)} core component files.")

        for f in target_files:
            path_str = str(f)
            try:
                if f.suffix == '.cs':
                    instance.add_csharp_file(path_str)
                else:
                    instance.add_xaml_file(path_str)
            except Exception as e:
                print(f"  [warn] {path_str}: {e}")

        instance.link_cross_references()
        print(f"[WpfAstGraph] Sub-Graph: {instance.G.number_of_nodes()} nodes, "
              f"{instance.G.number_of_edges()} edges")
        return instance

    def add_csharp_file(self, file_path: str) -> None:
        cs_file = CSharpParser.parse_file(file_path)
        self._cs_files.append(cs_file)

        file_node_id = f"file::{file_path}"
        self.G.add_node(file_node_id, kind='file',
                        label=Path(file_path).name, file=file_path)

        for cls in cs_file.classes:
            self._ingest_class(cls, cs_file, file_node_id)

    def add_xaml_file(self, file_path: str) -> None:
        xf = XamlParser.parse_file(file_path)
        self._xaml_files.append(xf)

        node_id = _xaml_id(xf.x_class, xf.path)
        self.G.add_node(node_id, **_xaml_attrs(xf))

        self._xaml_by_file[file_path] = node_id
        if xf.x_class:
            self._xaml_by_class[xf.x_class] = node_id

        # Add named controls as child nodes
        for ctrl_name, ctrl_node in xf.named_elements.items():
            ctrl_id = _ctrl_id(node_id, ctrl_node)
            self.G.add_node(ctrl_id, kind='xaml_control',
                            label=ctrl_name, tag=ctrl_node.tag,
                            file=file_path, line=ctrl_node.line,
                            bindings=[b.source_path for b in ctrl_node.bindings],
                            commands=[c[1] for c in ctrl_node.commands])
            self.G.add_edge(node_id, ctrl_id, rel='contains')

        # Resource dictionary merges
        for src in xf.resource_dictionary_merges:
            rd_id = f"resource_dict::{src}"
            if not self.G.has_node(rd_id):
                self.G.add_node(rd_id, kind='resource_dict', label=src, file=src)
            self.G.add_edge(node_id, rd_id, rel='uses_resource')

    def _ingest_class(self, cls: CSharpClass, cs_file: CSharpFile,
                      file_node_id: str) -> None:
        cid = _class_id(cls.namespace, cls.name)
        self.G.add_node(cid, **_class_attrs(cls, cs_file.path))
        self._class_by_name[cls.name] = cid
        self._class_by_id[cid] = cid

        self.G.add_edge(file_node_id, cid, rel='contains')

        # Inheritance
        if cls.base_class:
            base_name = cls.base_class.split('<')[0].strip()
            self.G.add_edge(cid, base_name, rel='inherits',
                            _deferred=True, _target_name=base_name)

        # Interface implementation
        for iface in cls.interfaces:
            iface_name = iface.split('<')[0].strip()
            self.G.add_edge(cid, iface_name, rel='implements',
                            _deferred=True, _target_name=iface_name)

        # Flattened class-to-class dependencies (no method child nodes)
        for method in cls.methods:
            # DI / constructor dependencies (ctor params)
            if method.name == cls.name:
                for param in method.parameters:
                    ptype = param.type_name.lstrip('I')  # strip leading I for interfaces
                    self.G.add_edge(cid, ptype, rel='depends_on',
                                    _deferred=True, _target_name=ptype,
                                    param_name=param.name)

            for inst in method.instantiations:
                self.G.add_edge(cid, inst, rel='instantiates',
                                _deferred=True, _target_name=inst)

        # Properties → type references
        for prop in cls.properties:
            ptype = re.sub(r'[<>?,\[\]\s]', '', prop.type_name)
            if ptype and ptype[0].isupper():
                self.G.add_edge(cid, ptype, rel='references',
                                _deferred=True, _target_name=ptype,
                                via_property=prop.name)

    # ── Cross-reference resolution ─────────────────────────────────────────────

    def link_cross_references(self) -> None:
        """
        Resolve deferred edges (whose targets were simple names, not full IDs).
        Also link XAML ↔ code-behind, bindings ↔ ViewModel properties.
        """
        G = self.G
        deferred = [(u, v, d) for u, v, d in G.edges(data=True) if d.get('_deferred')]

        edges_to_add:    list[tuple[str, str, dict]] = []
        edges_to_remove: list[tuple[str, str]] = []

        for u, v_name, data in deferred:
            resolved = self._resolve_name(str(v_name))
            edges_to_remove.append((u, v_name))
            if resolved:
                clean_data = {k: val for k, val in data.items()
                              if not k.startswith('_')}
                edges_to_add.append((u, resolved, clean_data))

        for u, v in edges_to_remove:
            if G.has_edge(u, v):
                G.remove_edge(u, v)
            # Remove orphan placeholder nodes
            if v in G and G.nodes[v].get('kind') is None:
                try:
                    G.remove_node(v)
                except Exception:
                    pass

        for u, v, d in edges_to_add:
            G.add_edge(u, v, **d)

        # XAML ↔ code-behind
        for xf in self._xaml_files:
            if not xf.x_class:
                continue
            xaml_id = self._xaml_by_class.get(xf.x_class)
            cs_id   = self._class_by_name.get(xf.x_class.split('.')[-1])
            if xaml_id and cs_id:
                G.add_edge(xaml_id, cs_id, rel='part_of_xaml')
                G.add_edge(cs_id, xaml_id, rel='part_of_xaml')

        # XAML data binding → ViewModel
        for xf in self._xaml_files:
            xaml_node_id = self._xaml_by_class.get(xf.x_class or '') or \
                           self._xaml_by_file.get(xf.path, '')
            if not xaml_node_id:
                continue

            # DataContext binding
            if xf.data_context_type:
                vm_name = xf.data_context_type.split('.')[-1]
                vm_id   = self._class_by_name.get(vm_name)
                if vm_id:
                    G.add_edge(xaml_node_id, vm_id, rel='data_context')

            # Command bindings → ViewModel class
            for (elem_name, cmd_path) in xf.all_commands:
                ctrl_id_str = f"{xaml_node_id}::{elem_name}"
                src = ctrl_id_str if G.has_node(ctrl_id_str) else xaml_node_id
                
                # Link directly to the ViewModel code-behind instead of method
                cs_id = self._class_by_name.get(xf.x_class.split('.')[-1] if xf.x_class else "")
                if cs_id:
                    G.add_edge(src, cs_id, rel='commands', binding_path=cmd_path)

            # Event handlers → code-behind class
            for (elem_name, event_name, handler_name) in xf.all_event_handlers:
                ctrl_id_str = f"{xaml_node_id}::{elem_name}"
                src = ctrl_id_str if G.has_node(ctrl_id_str) else xaml_node_id
                
                cs_id = self._class_by_name.get(xf.x_class.split('.')[-1] if xf.x_class else "")
                if cs_id:
                    G.add_edge(src, cs_id, rel='handles_event',
                               event=event_name, handler=handler_name)

    def _resolve_name(self, name: str) -> Optional[str]:
        """Try to resolve a simple or dotted name to a graph node ID."""
        # Already a node
        if name in self.G:
            return name
        # Simple name lookup
        if name in self._class_by_name:
            return self._class_by_name[name]
        # Strip generic args: List<Customer> → Customer
        stripped = re.sub(r'<.*>', '', name).strip()
        if stripped in self._class_by_name:
            return self._class_by_name[stripped]
        # Method lookup
        if name in self._method_by_name:
            ids = self._method_by_name[name]
            return ids[0] if ids else None
        return None

    # ── Query API ──────────────────────────────────────────────────────────────

    def find_node(self, name: str) -> Optional[str]:
        """Find a node ID by class name, method name, or XAML x:Class."""
        if name in self.G:
            return name
        if name in self._class_by_name:
            return self._class_by_name[name]
        if name in self._xaml_by_class:
            return self._xaml_by_class[name]
        # Partial match
        for nid in self.G.nodes():
            label = self.G.nodes[nid].get('label', '')
            if label == name or nid.endswith(f'.{name}') or nid.endswith(f'::{name}'):
                return nid
        return None

    def get_related(self, node_id: str, depth: int = 2,
                    rel_filter: list[str] = None) -> dict:
        """
        Return all nodes related to node_id within `depth` hops.
        Follows edges in BOTH directions (predecessors + successors).

        rel_filter: if given, only traverse edges with these 'rel' values.

        Returns a dict with:
          'center': node_id,
          'nodes':  {node_id: attrs, ...},
          'edges':  [(src, tgt, rel), ...],
        """
        visited_nodes: dict[str, dict] = {}
        visited_edges: list[tuple[str, str, str]] = []
        queue = [(node_id, 0)]
        seen  = {node_id}

        while queue:
            current, d = queue.pop(0)
            if current in self.G:
                visited_nodes[current] = dict(self.G.nodes[current])
            if d >= depth:
                continue

            # Outgoing edges
            for _, tgt, data in self.G.out_edges(current, data=True):
                rel = data.get('rel', '')
                if rel_filter and rel not in rel_filter:
                    continue
                edge = (current, tgt, rel)
                if edge not in visited_edges:
                    visited_edges.append(edge)
                if tgt not in seen:
                    seen.add(tgt)
                    queue.append((tgt, d + 1))

            # Incoming edges
            for src, _, data in self.G.in_edges(current, data=True):
                rel = data.get('rel', '')
                if rel_filter and rel not in rel_filter:
                    continue
                edge = (src, current, rel)
                if edge not in visited_edges:
                    visited_edges.append(edge)
                if src not in seen:
                    seen.add(src)
                    queue.append((src, d + 1))

        return {
            'center': node_id,
            'nodes':  visited_nodes,
            'edges':  visited_edges,
        }

    def subgraph_for(self, node_id: str, depth: int = 2) -> nx.DiGraph:
        """Return a NetworkX subgraph for the related nodes."""
        result = self.get_related(node_id, depth)
        return self.G.subgraph(list(result['nodes'].keys()))

    def stats(self) -> dict:
        G = self.G
        kinds: dict[str, int] = {}
        rels:  dict[str, int] = {}
        for _, attrs in G.nodes(data=True):
            k = attrs.get('kind', 'unknown')
            kinds[k] = kinds.get(k, 0) + 1
        for _, _, attrs in G.edges(data=True):
            r = attrs.get('rel', 'unknown')
            rels[r] = rels.get(r, 0) + 1
        return {
            'total_nodes': G.number_of_nodes(),
            'total_edges': G.number_of_edges(),
            'node_kinds':  kinds,
            'edge_rels':   rels,
        }

    def to_json(self) -> dict:
        """Serialise the full graph to a JSON-safe dict."""
        from networkx.readwrite import json_graph
        return json_graph.node_link_data(self.G)

    def save(self, path: str) -> None:
        """Save graph to a GraphML or JSON file."""
        p = Path(path)
        if p.suffix == '.graphml':
            # Convert list attrs to strings for GraphML
            G2 = nx.DiGraph()
            for n, d in self.G.nodes(data=True):
                safe = {k: (str(v) if isinstance(v, (list, dict)) else v)
                        for k, v in d.items()}
                G2.add_node(n, **safe)
            for u, v, d in self.G.edges(data=True):
                safe = {k: (str(v) if isinstance(v, (list, dict)) else v)
                        for k, v in d.items()}
                G2.add_edge(u, v, **safe)
            nx.write_graphml(G2, path)
        elif p.suffix == '.json':
            import json
            p.write_text(json.dumps(self.to_json(), indent=2))
        print(f"[WpfAstGraph] Saved to {path}")
