"""
csharp_parser.py
----------------
Regex-based C# AST parser that extracts:
  - Classes, structs, interfaces, enums
  - Properties, fields, methods, constructors
  - Base types, interfaces implemented
  - Method calls, object instantiations
  - Dependency injection (constructor params)
  - Namespace and using declarations
  - Attributes/decorators
  - Events and delegates
  - Generic type parameters
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class CSharpField:
    name: str
    type_name: str
    modifiers: list[str]
    is_readonly: bool
    line: int
    raw: str = ""

@dataclass
class CSharpProperty:
    name: str
    type_name: str
    modifiers: list[str]
    has_getter: bool
    has_setter: bool
    is_auto: bool
    line: int
    attributes: list[str] = field(default_factory=list)
    raw: str = ""

@dataclass
class CSharpParameter:
    name: str
    type_name: str
    has_default: bool = False
    default_value: str = ""

@dataclass
class CSharpMethod:
    name: str
    return_type: str
    modifiers: list[str]
    parameters: list[CSharpParameter]
    line: int
    calls: list[str] = field(default_factory=list)          # methods called inside body
    instantiations: list[str] = field(default_factory=list) # types newed up
    attributes: list[str] = field(default_factory=list)
    is_async: bool = False
    is_override: bool = False
    body_raw: str = ""

@dataclass
class CSharpEvent:
    name: str
    delegate_type: str
    modifiers: list[str]
    line: int

@dataclass
class CSharpClass:
    name: str
    namespace: str
    kind: str                           # "class" | "interface" | "struct" | "enum" | "record"
    modifiers: list[str]
    base_class: Optional[str]
    interfaces: list[str]
    type_parameters: list[str]
    attributes: list[str]
    fields: list[CSharpField]
    properties: list[CSharpProperty]
    methods: list[CSharpMethod]
    events: list[CSharpEvent]
    inner_classes: list[str]           # names only; full parse would recurse
    line: int
    file_path: str

@dataclass
class CSharpFile:
    path: str
    namespace: str
    usings: list[str]
    classes: list[CSharpClass]
    top_level_statements: bool         # C# 9+ style


# ── Helpers ────────────────────────────────────────────────────────────────────

_MODIFIERS = {"public", "private", "protected", "internal", "static", "abstract",
              "virtual", "override", "sealed", "readonly", "extern", "partial",
              "async", "new", "unsafe", "volatile"}

def _strip_comments(src: str) -> str:
    """Remove // line comments and /* */ block comments."""
    src = re.sub(r'//[^\n]*', '', src)
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    return src

def _extract_block(src: str, start: int) -> tuple[str, int]:
    """
    Given src and the index of an opening '{', return (block_content, end_index).
    Handles nested braces. Returns ("", start) if no '{' found from start.
    """
    depth = 0
    i = start
    begin = -1
    while i < len(src):
        c = src[i]
        if c == '{':
            depth += 1
            if depth == 1:
                begin = i
        elif c == '}':
            depth -= 1
            if depth == 0 and begin != -1:
                return src[begin+1:i], i
        i += 1
    return "", start

def _parse_parameters(param_str: str) -> list[CSharpParameter]:
    """Parse a raw parameter list string into CSharpParameter objects."""
    params: list[CSharpParameter] = []
    if not param_str.strip():
        return params
    # Split by commas not inside angle brackets or parens
    parts = _split_respecting_generics(param_str, ',')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip attributes like [FromBody], [NotNull] etc.
        part = re.sub(r'\[.*?\]', '', part).strip()
        # Strip modifiers like ref, out, in, params, this
        part = re.sub(r'^(ref|out|in|params|this)\s+', '', part)
        # Optional default value
        default_val = ""
        has_default = False
        if '=' in part:
            part, default_val = part.rsplit('=', 1)
            part = part.strip()
            default_val = default_val.strip()
            has_default = True
        tokens = part.rsplit(None, 1)
        if len(tokens) == 2:
            params.append(CSharpParameter(
                name=tokens[1], type_name=tokens[0],
                has_default=has_default, default_value=default_val
            ))
        elif len(tokens) == 1:
            params.append(CSharpParameter(name=tokens[0], type_name="unknown"))
    return params

def _split_respecting_generics(s: str, sep: str) -> list[str]:
    """Split string by sep, but ignore sep inside < > and ( )."""
    parts, depth, current = [], 0, []
    for c in s:
        if c in '<(' : depth += 1
        elif c in '>)': depth -= 1
        if c == sep and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(c)
    if current:
        parts.append(''.join(current))
    return parts

def _extract_calls_from_body(body: str) -> tuple[list[str], list[str]]:
    """Return (method_calls, instantiations) found in a method body."""
    # Method calls: Foo(...) or obj.Foo(...) or await Foo(...)
    call_pat = re.compile(r'\b([A-Z][a-zA-Z0-9_]*(?:\.[A-Za-z][a-zA-Z0-9_]*)*)\s*\(')
    calls = list({m.group(1) for m in call_pat.finditer(body)
                  if not m.group(1)[0].islower()})  # skip built-ins

    # Instantiations: new SomeType(...)  or  new SomeType<T>(...)
    inst_pat = re.compile(r'\bnew\s+([A-Z][a-zA-Z0-9_<>,\s\[\]]*?)(?:\s*\(|\s*{)')
    insts = list({re.sub(r'<.*>', '', m.group(1)).strip() for m in inst_pat.finditer(body)})

    return calls, insts

def _extract_attributes(src: str, pos: int) -> list[str]:
    """Look backwards from pos for [Attribute] decorators."""
    attrs = []
    chunk = src[max(0, pos-400):pos]
    for m in re.finditer(r'\[([^\[\]]+)\]', chunk):
        text = m.group(1).strip()
        if not text.startswith('assembly:') and not text.startswith('module:'):
            attrs.append(text.split('(')[0].strip())
    return attrs

def _line_number(src: str, pos: int) -> int:
    return src[:pos].count('\n') + 1


# ── Main parser ────────────────────────────────────────────────────────────────

class CSharpParser:
    """
    Parse a single .cs file into a CSharpFile dataclass.
    Usage:
        result = CSharpParser.parse_file("/path/to/MyClass.cs")
    """

    # Patterns compiled once
    _NS_PAT      = re.compile(r'\bnamespace\s+([\w.]+)\s*[{;]')
    _USING_PAT   = re.compile(r'\busing\s+(?:static\s+)?(?:[\w.]+\s*=\s*)?([\w.]+);')
    _CLASS_PAT   = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|abstract|'
        r'sealed|partial|readonly|unsafe|new)\s+)*)'
        r'(?P<kind>class|struct|interface|enum|record)\s+'
        r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
        r'(?P<tparams><[^>]+>)?'
        r'(?:\s*:\s*(?P<bases>[^{]+))?'
        r'\s*\{'
    )
    _FIELD_PAT   = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|readonly|'
        r'const|volatile|new|unsafe)\s+)+)'
        r'(?P<type>[\w<>\[\],\s\?\.]+?)\s+'
        r'(?P<name>[a-z_][A-Za-z0-9_]*)\s*'
        r'(?:=\s*[^;]+)?;'
    )
    _PROP_PAT    = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|virtual|'
        r'override|abstract|new|readonly|required)\s+)*)'
        r'(?P<type>[\w<>\[\],\s\?\.]+?)\s+'
        r'(?P<name>[A-Z][A-Za-z0-9_]*)\s*'
        r'\{(?P<accessors>[^}]*)\}'
    )
    _EXPR_PROP_PAT = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|virtual|'
        r'override|abstract|new|readonly|required)\s+)*)'
        r'(?P<type>[\w<>\[\],\s\?\.]+?)\s+'
        r'(?P<name>[A-Z][A-Za-z0-9_]*)\s*=>\s*'
    )
    _METHOD_PAT  = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|virtual|'
        r'override|abstract|async|new|unsafe|extern|partial)\s+)*)'
        r'(?P<ret>[\w<>\[\],\s\?\.]+?)\s+'
        r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*'
        r'(?P<tparams><[^>]+>)?\s*'
        r'\((?P<params>[^)]*)\)\s*'
        r'(?:where\s+[^{;]+)?\s*'
        r'[{;]'
    )
    _EVENT_PAT   = re.compile(
        r'(?P<mods>(?:(?:public|private|protected|internal|static|virtual|'
        r'override|abstract)\s+)*)'
        r'event\s+'
        r'(?P<type>[\w<>\[\],\s\?\.]+?)\s+'
        r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*[;{]'
    )

    @classmethod
    def parse_file(cls, file_path: str) -> CSharpFile:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(file_path)
        raw = path.read_text(encoding='utf-8', errors='replace')
        return cls._parse(raw, str(path))

    @classmethod
    def parse_source(cls, source: str, file_path: str = "<memory>") -> CSharpFile:
        return cls._parse(source, file_path)

    @classmethod
    def _parse(cls, raw: str, file_path: str) -> CSharpFile:
        clean = _strip_comments(raw)

        # Namespace
        ns_m = cls._NS_PAT.search(clean)
        namespace = ns_m.group(1) if ns_m else ""

        # Usings
        usings = [m.group(1) for m in cls._USING_PAT.finditer(clean)]

        # Classes / structs / interfaces / enums
        classes: list[CSharpClass] = []
        for cm in cls._CLASS_PAT.finditer(clean):
            cls_obj = cls._parse_class(cm, clean, file_path, namespace)
            if cls_obj:
                classes.append(cls_obj)

        top_level = (not classes and not ns_m and
                     bool(re.search(r'^\s*(var|int|string|Console\.|await)\b', clean, re.M)))

        return CSharpFile(
            path=file_path,
            namespace=namespace,
            usings=usings,
            classes=classes,
            top_level_statements=top_level,
        )

    @classmethod
    def _parse_class(cls, cm: re.Match, src: str, file_path: str, namespace: str
                     ) -> Optional[CSharpClass]:
        mods_raw = cm.group('mods') or ""
        mods     = [t for t in mods_raw.split() if t in _MODIFIERS]
        kind     = cm.group('kind')
        name     = cm.group('name')
        tparams_raw = cm.group('tparams') or ""
        tparams  = [t.strip() for t in tparams_raw.strip('<>').split(',')] if tparams_raw else []
        bases_raw = (cm.group('bases') or "").strip()

        base_class: Optional[str] = None
        interfaces: list[str] = []
        if bases_raw:
            parts = [p.strip() for p in _split_respecting_generics(bases_raw, ',')]
            for i, p in enumerate(parts):
                p_name = p.split('<')[0].strip()
                if i == 0 and kind in ('class', 'struct', 'record') and not p_name.startswith('I'):
                    base_class = p
                else:
                    interfaces.append(p)

        attrs    = _extract_attributes(src, cm.start())
        line     = _line_number(src, cm.start())

        # Extract class body
        body, body_end = _extract_block(src, cm.end() - 1)

        # Parse members
        fields     = cls._parse_fields(body, line)
        properties = cls._parse_properties(body, line)
        methods    = cls._parse_methods(body, line, name)
        events     = cls._parse_events(body, line)

        # Inner class names
        inner = [m.group('name') for m in cls._CLASS_PAT.finditer(body)]

        return CSharpClass(
            name=name, namespace=namespace, kind=kind, modifiers=mods,
            base_class=base_class, interfaces=interfaces, type_parameters=tparams,
            attributes=attrs, fields=fields, properties=properties,
            methods=methods, events=events, inner_classes=inner,
            line=line, file_path=file_path,
        )

    @classmethod
    def _parse_fields(cls, body: str, base_line: int) -> list[CSharpField]:
        fields: list[CSharpField] = []
        # Only match lines that look like field declarations (lowercase first char name)
        for m in cls._FIELD_PAT.finditer(body):
            name     = m.group('name').strip()
            type_name = m.group('type').strip()
            mods_raw = m.group('mods').strip()
            mods     = [t for t in mods_raw.split() if t in _MODIFIERS]
            # Exclude keywords mistaken for fields
            if type_name in ('return', 'throw', 'if', 'else', 'for', 'while', 'switch'):
                continue
            fields.append(CSharpField(
                name=name, type_name=type_name, modifiers=mods,
                is_readonly='readonly' in mods or 'const' in mods,
                line=base_line + _line_number(body, m.start()) - 1,
                raw=m.group(0),
            ))
        return fields

    @classmethod
    def _parse_properties(cls, body: str, base_line: int) -> list[CSharpProperty]:
        props: list[CSharpProperty] = []
        for m in cls._PROP_PAT.finditer(body):
            name     = m.group('name').strip()
            type_name = m.group('type').strip()
            mods_raw = m.group('mods').strip()
            mods     = [t for t in mods_raw.split() if t in _MODIFIERS]
            accessors = m.group('accessors') or ""
            has_get  = 'get' in accessors
            has_set  = 'set' in accessors or 'init' in accessors
            is_auto  = bool(re.search(r'get\s*;|set\s*;|init\s*;', accessors))
            attrs    = _extract_attributes(body, m.start())
            if type_name in ('return', 'throw', 'if', 'else', 'for'):
                continue
            props.append(CSharpProperty(
                name=name, type_name=type_name, modifiers=mods,
                has_getter=has_get, has_setter=has_set, is_auto=is_auto,
                line=base_line + _line_number(body, m.start()) - 1,
                attributes=attrs, raw=m.group(0),
            ))

        # Expression-bodied properties: public string Name => _name;
        for m in cls._EXPR_PROP_PAT.finditer(body):
            name = m.group('name').strip()
            type_name = m.group('type').strip()
            mods_raw = m.group('mods').strip()
            mods = [t for t in mods_raw.split() if t in _MODIFIERS]
            if type_name in ('return', 'throw', 'if', 'else', 'for'):
                continue
            # Skip if already captured by the standard property regex
            if any(p.name == name for p in props):
                continue
            props.append(CSharpProperty(
                name=name, type_name=type_name, modifiers=mods,
                has_getter=True, has_setter=False, is_auto=False,
                line=base_line + _line_number(body, m.start()) - 1,
                attributes=[], raw=m.group(0),
            ))

        return props

    @classmethod
    def _parse_methods(cls, body: str, base_line: int, class_name: str) -> list[CSharpMethod]:
        methods: list[CSharpMethod] = []
        seen_names: set[str] = set()

        for m in cls._METHOD_PAT.finditer(body):
            mods_raw  = m.group('mods').strip()
            mods      = [t for t in mods_raw.split() if t in _MODIFIERS]
            ret       = m.group('ret').strip()
            name      = m.group('name').strip()
            params_raw = m.group('params')

            # Filter false-positives
            if ret in ('return', 'throw', 'if', 'else', 'for', 'while', 'switch',
                       'using', 'var', 'new', 'case', 'catch', 'finally'):
                continue
            if name in ('if', 'else', 'for', 'while', 'switch', 'catch', 'finally',
                        'using', 'return', 'throw', 'new', 'class'):
                continue

            dedup_key = f"{name}({params_raw})"
            if dedup_key in seen_names:
                continue
            seen_names.add(dedup_key)

            params  = _parse_parameters(params_raw)
            is_ctor = (name == class_name)
            is_async = 'async' in mods

            # Extract body
            brace_pos = body.find('{', m.end() - 1)
            method_body = ""
            if brace_pos != -1:
                method_body, _ = _extract_block(body, brace_pos)
            calls, insts = _extract_calls_from_body(method_body)
            attrs = _extract_attributes(body, m.start())

            line = base_line + _line_number(body, m.start()) - 1
            methods.append(CSharpMethod(
                name=name, return_type="(ctor)" if is_ctor else ret,
                modifiers=mods, parameters=params, line=line,
                calls=calls, instantiations=insts,
                attributes=attrs, is_async=is_async,
                is_override='override' in mods, body_raw=method_body,
            ))
        return methods

    @classmethod
    def _parse_events(cls, body: str, base_line: int) -> list[CSharpEvent]:
        events: list[CSharpEvent] = []
        for m in cls._EVENT_PAT.finditer(body):
            mods = [t for t in (m.group('mods') or "").split() if t in _MODIFIERS]
            events.append(CSharpEvent(
                name=m.group('name').strip(),
                delegate_type=m.group('type').strip(),
                modifiers=mods,
                line=base_line + _line_number(body, m.start()) - 1,
            ))
        return events
