"""
xaml_parser.py
--------------
Parses WPF .xaml files into a structured tree of XamlNode objects capturing:
  - Control hierarchy (parent/child relationships)
  - Data bindings ({Binding Path=...})
  - Static resources ({StaticResource ...})
  - x:Name / Name attributes (wire-up identifiers)
  - Command bindings (Command="{Binding XxxCommand}")
  - DataContext assignments
  - Event handler references (Click="OnSave_Click")
  - x:Class attribute (links XAML to its code-behind)
  - Style and ControlTemplate references
  - NavigationService.Navigate / Frame source refs
"""

from __future__ import annotations
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class XamlBinding:
    target_property: str          # e.g. "Text"
    source_path: str              # e.g. "Customer.FullName"
    mode: str                     # OneWay | TwoWay | OneTime | etc.
    converter: Optional[str]
    element_name: Optional[str]   # ElementName binding
    relative_source: Optional[str]
    update_trigger: Optional[str]

@dataclass
class XamlResource:
    key: str
    kind: str                     # "StaticResource" | "DynamicResource" | "x:Static"

@dataclass
class XamlNode:
    tag: str                      # local name, e.g. "Button", "Grid", "TextBox"
    full_tag: str                 # with namespace, e.g. "wpf:MyControl"
    name: Optional[str]           # x:Name or Name attribute
    x_class: Optional[str]        # only on root element
    data_context: Optional[str]   # DataContext="{Binding ...}" or static class
    attributes: dict[str, str]
    bindings: list[XamlBinding]
    resources_used: list[XamlResource]
    event_handlers: list[tuple[str, str]]  # (event_name, handler_method)
    commands: list[tuple[str, str]]         # (prop_name, binding_path)
    children: list["XamlNode"]
    parent_tag: Optional[str]
    line: int

@dataclass
class XamlFile:
    path: str
    x_class: Optional[str]        # e.g. "MyApp.Views.CustomerFormView"
    root_tag: str
    data_context_type: Optional[str]
    resource_dictionary_merges: list[str]  # merged dict Sources
    nodes: list[XamlNode]          # flat list (for easy search)
    root_node: Optional[XamlNode]
    named_elements: dict[str, XamlNode]   # x:Name → node
    all_bindings: list[XamlBinding]
    all_event_handlers: list[tuple[str, str, str]]  # (element_name, event, handler)
    all_commands: list[tuple[str, str]]             # (element_name, command_path)


# ── Binding expression parser ──────────────────────────────────────────────────

_BINDING_RE = re.compile(
    r'\{(?:Binding|binding)\s*(?P<rest>[^}]*)\}', re.IGNORECASE
)
_STATIC_RES_RE  = re.compile(r'\{(?:Static|Dynamic)Resource\s+(?P<key>\w+)\}')
_X_STATIC_RE    = re.compile(r'\{x:Static\s+(?P<ref>[\w.]+)\}')

def _parse_binding(prop: str, expr: str) -> Optional[XamlBinding]:
    m = _BINDING_RE.match(expr.strip())
    if not m:
        return None
    rest = m.group('rest').strip()
    # Build a mini key=value parser
    kv: dict[str, str] = {}
    # Path can be positional first arg
    if rest and not rest.startswith(',') and '=' not in rest.split(',')[0]:
        first = rest.split(',')[0].strip()
        kv['Path'] = first
        rest = ','.join(rest.split(',')[1:])
    for part in re.split(r',(?![^{]*\})', rest):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            kv[k.strip()] = v.strip()
    return XamlBinding(
        target_property=prop,
        source_path=kv.get('Path', ''),
        mode=kv.get('Mode', 'OneWay'),
        converter=kv.get('Converter', None),
        element_name=kv.get('ElementName', None),
        relative_source=kv.get('RelativeSource', None),
        update_trigger=kv.get('UpdateSourceTrigger', None),
    )

def _is_event_handler(value: str, prop: str) -> bool:
    """Heuristic: value looks like a code-behind method name."""
    return (
        bool(re.match(r'^On[A-Z]|_(?:Click|Changed|Selected|Loaded|Executed|'
                      r'CanExecute|KeyDown|KeyUp|MouseDown|MouseUp|TextChanged)$', value))
        and not value.startswith('{')
    )

def _is_command_prop(prop: str) -> bool:
    return 'Command' in prop and 'CommandParameter' not in prop

def _parse_resource_refs(value: str) -> list[XamlResource]:
    refs = []
    for m in _STATIC_RES_RE.finditer(value):
        refs.append(XamlResource(key=m.group('key'), kind='StaticResource'))
    for m in _X_STATIC_RE.finditer(value):
        refs.append(XamlResource(key=m.group('ref'), kind='x:Static'))
    return refs


# ── Main parser ────────────────────────────────────────────────────────────────

# Known WPF namespaces → strip for local tag
_NS_MAP = {
    'http://schemas.microsoft.com/winfx/2006/xaml/presentation': '',
    'http://schemas.microsoft.com/winfx/2006/xaml': 'x',
    'http://schemas.microsoft.com/expression/blend/2008': 'd',
    'http://schemas.openxmlformats.org/markup-compatibility/2006': 'mc',
}

def _local(tag: str) -> str:
    m = re.match(r'\{([^}]+)\}(.+)', tag)
    if m:
        ns_prefix = _NS_MAP.get(m.group(1), m.group(1).split('/')[-1])
        local = m.group(2)
        return f"{ns_prefix}:{local}" if ns_prefix else local
    return tag


class XamlParser:
    """
    Parse a single WPF .xaml file into a XamlFile dataclass.

    Usage:
        result = XamlParser.parse_file("/path/to/CustomerForm.xaml")
    """

    @classmethod
    def parse_file(cls, file_path: str) -> XamlFile:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(file_path)
        src = path.read_text(encoding='utf-8', errors='replace')
        return cls.parse_source(src, str(path))

    @classmethod
    def parse_source(cls, src: str, file_path: str = "<memory>") -> XamlFile:
        try:
            root_elem = ET.fromstring(src)
        except ET.ParseError as e:
            # Return a minimal stub on parse error
            return XamlFile(
                path=file_path, x_class=None, root_tag="(parse_error)",
                data_context_type=None, resource_dictionary_merges=[],
                nodes=[], root_node=None, named_elements={},
                all_bindings=[], all_event_handlers=[], all_commands=[],
            )

        # Build line number map by scanning raw XML
        line_map = cls._build_line_map(src)

        all_nodes: list[XamlNode] = []
        named:     dict[str, XamlNode] = {}
        all_bindings: list[XamlBinding] = []
        all_handlers: list[tuple[str, str, str]] = []
        all_commands: list[tuple[str, str]] = []
        resource_merges: list[str] = []

        def walk(elem: ET.Element, parent_tag: Optional[str], depth: int) -> XamlNode:
            tag      = _local(elem.tag)
            attrs    = {_local(k): v for k, v in elem.attrib.items()}

            # x:Name / Name
            name = attrs.get('x:Name') or attrs.get('Name')
            # x:Class on root
            x_class = attrs.get('x:Class') if depth == 0 else None
            # DataContext
            dc = attrs.get('DataContext', '')
            data_ctx: Optional[str] = None
            if dc:
                bnd = _parse_binding('DataContext', dc)
                data_ctx = bnd.source_path if bnd else dc

            bindings:  list[XamlBinding] = []
            resources: list[XamlResource] = []
            handlers:  list[tuple[str, str]] = []
            commands:  list[tuple[str, str]] = []

            for attr_name, attr_val in attrs.items():
                # Bindings
                if '{Binding' in attr_val or '{binding' in attr_val:
                    bnd = _parse_binding(attr_name, attr_val)
                    if bnd:
                        bindings.append(bnd)
                        all_bindings.append(bnd)
                        if _is_command_prop(attr_name):
                            cmd_path = bnd.source_path
                            commands.append((attr_name, cmd_path))
                            all_commands.append((name or tag, cmd_path))

                # Resource references
                resources.extend(_parse_resource_refs(attr_val))

                # Event handlers
                if _is_event_handler(attr_val, attr_name):
                    handlers.append((attr_name, attr_val))
                    all_handlers.append((name or tag, attr_name, attr_val))

                # ResourceDictionary Source merge
                if attr_name == 'Source' and 'ResourceDictionary' in (parent_tag or ''):
                    resource_merges.append(attr_val)

            line = line_map.get(elem.tag + str(list(elem.attrib.items()))[:40], 0)

            node = XamlNode(
                tag=tag, full_tag=elem.tag, name=name, x_class=x_class,
                data_context=data_ctx, attributes=attrs,
                bindings=bindings, resources_used=resources,
                event_handlers=handlers, commands=commands,
                children=[], parent_tag=parent_tag, line=line,
            )
            all_nodes.append(node)
            if name:
                named[name] = node

            for child in elem:
                child_node = walk(child, tag, depth + 1)
                node.children.append(child_node)

            return node

        root_node   = walk(root_elem, None, 0)
        x_class     = root_node.x_class
        root_dc     = root_node.data_context

        # Detect DC type from Binding path like "Source={StaticResource Locator}"
        dc_type = root_dc

        return XamlFile(
            path=file_path,
            x_class=x_class,
            root_tag=root_node.tag,
            data_context_type=dc_type,
            resource_dictionary_merges=resource_merges,
            nodes=all_nodes,
            root_node=root_node,
            named_elements=named,
            all_bindings=all_bindings,
            all_event_handlers=all_handlers,
            all_commands=all_commands,
        )

    @staticmethod
    def _build_line_map(src: str) -> dict[str, int]:
        """Simple line-number lookup via scanning for tag openings."""
        mapping: dict[str, int] = {}
        for i, line in enumerate(src.splitlines(), 1):
            m = re.search(r'<(\w[\w:]*)', line)
            if m:
                key = m.group(0)
                if key not in mapping:
                    mapping[key] = i
        return mapping
