"""Conservative, pure-source Java technology evidence adapters.

These are lexical recognizers, not a Java compiler. Unresolved imports, shadowed
types and incomplete declarations are omitted rather than guessed. No I/O.
"""
import re

TECHNOLOGIES = {
    "SpringBoot": ("org.springframework.boot", "spring-boot"),
    "SpringMVC": ("org.springframework.web", "spring-webmvc", "spring-web"),
    "SpringDI": ("org.springframework.context", "spring-context", "spring-beans"),
    "MyBatis": ("org.mybatis", "org.apache.ibatis", "mybatis"),
    "MyBatisPlus": ("com.baomidou.mybatisplus", "mybatis-plus"),
}
_ADAPTERS = {"SpringBoot": "SpringBootAdapter", "SpringMVC": "SpringMvcAdapter",
             "SpringDI": "SpringDIAdapter", "MyBatis": "MyBatisAdapter", "MyBatisPlus": "MyBatisPlusAdapter"}
_ANNOTATIONS = {
    "org.springframework.boot.autoconfigure.SpringBootApplication": ("SpringBoot", "boot_application"),
    "org.springframework.stereotype.Controller": ("SpringMVC", "controller"),
    "org.springframework.web.bind.annotation.RestController": ("SpringMVC", "controller"),
    "org.springframework.beans.factory.annotation.Autowired": ("SpringDI", "injects"),
    "org.springframework.stereotype.Service": ("SpringDI", "service"),
    "org.springframework.stereotype.Component": ("SpringDI", "component"),
    "org.springframework.stereotype.Repository": ("SpringDI", "repository"),
    "org.springframework.context.annotation.Configuration": ("SpringDI", "configuration"),
    "org.springframework.context.annotation.Bean": ("SpringDI", "bean"),
    "org.mybatis.spring.annotation.MapperScan": ("MyBatis", "mapper_scan"),
    "org.apache.ibatis.annotations.Mapper": ("MyBatis", "mapper"),
    "org.apache.ibatis.annotations.Param": ("MyBatis", "parameter_binding"),
}
for _route in ("RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping"):
    _ANNOTATIONS["org.springframework.web.bind.annotation." + _route] = ("SpringMVC", "route")
_EXTENDS = {
    "com.baomidou.mybatisplus.core.mapper.BaseMapper": "maps_entity",
    "com.baomidou.mybatisplus.extension.service.IService": "service_entity",
    "com.baomidou.mybatisplus.extension.service.impl.ServiceImpl": "service_mapper",
}
_TOKEN = re.compile(r'//[^\n\r]*|/\*[\s\S]*?\*/|"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_IDENT = r"[A-Za-z_$][\w$]*"


def _mask(text, *, strings=True):
    def replace(match):
        token = match.group()
        if not strings and token[0] in "\"'":
            return token
        return "".join("\n" if ch == "\n" else "\r" if ch == "\r" else " " for ch in token)
    return _TOKEN.sub(replace, text)


def _source(path, text):
    if not isinstance(path, str) or not path.lower().endswith(".java") or not isinstance(text, str):
        return None
    code = _mask(text)
    imports = {}
    wildcards = []
    for match in re.finditer(r"\bimport\s+(?!static\b)([\w.$]+(?:\.\*)?)\s*;", code):
        name = match.group(1)
        if name.endswith(".*"):
            wildcards.append(name[:-2])
        else:
            imports.setdefault(name.rsplit(".", 1)[-1], set()).add(name)
    shadowed = set(re.findall(r"\b(?:class|interface|enum|record)\s+(" + _IDENT + ")", code))
    def qualified(name, candidates):
        if name in candidates:
            return name
        if "." in name or name in shadowed:
            return None
        explicit = imports.get(name, set())
        if len(explicit) == 1:
            target = next(iter(explicit))
            return target if target in candidates else None
        if explicit or len(wildcards) != 1:
            return None
        candidate = wildcards[0] + "." + name
        return candidate if candidate in candidates else None
    return code, qualified


def _annotations(code, qualify):
    found = []
    for match in re.finditer(r"@(" + _IDENT + r"(?:\." + _IDENT + r")*)", code):
        target = qualify(match.group(1), _ANNOTATIONS)
        if target is None:
            continue
        end = match.end()
        cursor = end
        while cursor < len(code) and code[cursor].isspace():
            cursor += 1
        if cursor < len(code) and code[cursor] == "(":
            depth = 1
            cursor += 1
            while cursor < len(code) and depth:
                depth += (code[cursor] == "(") - (code[cursor] == ")")
                cursor += 1
            if depth:
                continue
            end = cursor
        found.append((match.start(), end, match.group(1), target))
    return found


def _declaration(code, end):
    """Find the immediately annotated declaration, skipping other annotations."""
    tail = code[end:]
    cursor = 0
    while True:
        annotation = re.match(r"\s*@" + _IDENT + r"(?:\." + _IDENT + r")*\s*", tail[cursor:])
        if annotation is None:
            break
        cursor += annotation.end()
        if cursor < len(tail) and tail[cursor] == "(":
            depth = 1
            cursor += 1
            while cursor < len(tail) and depth:
                depth += (tail[cursor] == "(") - (tail[cursor] == ")")
                cursor += 1
            if depth:
                return None
    remainder = tail[cursor:]
    match = re.match(r"\s*(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed|default|synchronized)\s+)*(?P<kind>class|interface|enum|record)\s+(?P<name>" + _IDENT + ")", remainder)
    if match:
        return "type", match.group("name"), end + cursor + match.end()
    match = re.match(r"\s*(?:(?:public|protected|private|abstract|static|final|default|synchronized)\s+)*(?:[\w.$<>?,\[\] ]+)\s+(?P<name>" + _IDENT + r")\s*(?P<suffix>\(|[;=,)])", remainder)
    if match:
        return "method" if match.group("suffix") == "(" else "field", match.group("name"), end + cursor + match.end()
    return None


def _raw_matches(path, safe_text, enabled=None):
    eligible = set(TECHNOLOGIES) if enabled is None else set(enabled)
    if not eligible:
        return []
    parsed = _source(path, safe_text)
    if parsed is None:
        return []
    code, qualify = parsed
    clean_literals = _mask(safe_text, strings=False)
    annotations = _annotations(code, qualify)
    rows = []
    def emit(technology, start, end, symbol, relation, related=None, kind="source_usage"):
        if not symbol or symbol not in safe_text[start:end]:
            return
        row = {"technology": technology, "adapter_id": _ADAPTERS[technology], "adapter_version": "1",
               "evidence_type": kind, "symbol": symbol, "relation": relation,
               "char_start": start, "char_end": end}
        if related is not None:
            row["related_symbol"] = related
        rows.append(row)
    controllers = []
    for start, end, name, target in annotations:
        technology, relation = _ANNOTATIONS[target]
        if technology not in eligible:
            continue
        declaration = _declaration(code, end)
        if relation == "parameter_binding":
            literals = list(re.finditer(r'"([^"\\]+)"', clean_literals[start:end]))
            parameter = re.match(r"\s*(?:final\s+)?[\w.$<>?\[\]]+\s+(" + _IDENT + r")\s*(?=[,)])", code[end:])
            if literals and parameter:
                emit(technology, start, end + parameter.end(), literals[0].group(1), relation, parameter.group(1), "annotation_binding")
            continue
        if declaration is None:
            continue
        kind, declared, declaration_end = declaration
        if relation in {"boot_application", "controller", "service", "component", "repository", "configuration", "mapper", "mapper_scan"} and kind != "type":
            continue
        if relation == "bean" and kind != "method":
            continue
        if relation == "route":
            if kind not in {"type", "method"}:
                continue
            # Only recognized annotation source literals are route evidence. Never
            # synthesize a joined path not present in the supplied source.
            for literal in re.finditer(r'"([^"\\]*)"', clean_literals[start:end]):
                prefix = code[start:start + literal.start()]
                # Restrict named values; produces/consumes/header metadata is not a route.
                assignments = list(re.finditer(r"(\w+)\s*=", prefix))
                if assignments and assignments[-1].group(1) not in {"value", "path"}:
                    continue
                emit(technology, start, declaration_end, literal.group(1), "base_route" if kind == "type" else "method_route", declared, "route_declaration")
            # Actual @GetMapping usage qualifies MVC even when no literal path.
            emit(technology, start, declaration_end, name, "route_annotation", declared, "annotation_usage")
        elif relation == "mapper_scan":
            emit(technology, start, declaration_end, name, relation, declared, "annotation_usage")
            for literal in re.finditer(r'"([\w.]+)"', clean_literals[start:end]):
                assignments = list(re.finditer(r"(\w+)\s*=", code[start:start + literal.start()]))
                if assignments and assignments[-1].group(1) not in {"value", "basePackages"}:
                    continue
                emit(technology, start, declaration_end, literal.group(1), "mapper_package", declared, "annotation_binding")
        else:
            emit(technology, start, declaration_end, name, relation, declared, "annotation_usage")
            if relation == "controller":
                opening = code.find("{", declaration_end)
                if opening >= 0:
                    cursor, depth = opening + 1, 1
                    while cursor < len(code) and depth:
                        depth += (code[cursor] == "{") - (code[cursor] == "}")
                        cursor += 1
                    controllers.append((opening, cursor))
            if relation == "injects" and kind == "field":
                field = re.match(r"\s*(?:(?:private|protected|public|final|static)\s+)*(" + _IDENT + r"(?:\." + _IDENT + r")*)\s+(" + _IDENT + r")\s*[;=]", code[end:])
                if field:
                    emit("SpringDI", start, end + field.end(), field.group(2), "injected_type", field.group(1), "dependency_binding")
                    controller = next(((left, right) for left, right in controllers if left < start < right), None)
                    if "SpringMVC" in eligible and controller and re.search(r"\b" + re.escape(field.group(2)) + r"\s*\.\s*" + _IDENT + r"\s*\(", code[controller[0]:controller[1]]):
                        emit("SpringMVC", start, end + field.end(), field.group(2), "controller_dependency", field.group(1), "dependency_binding")
    if 'SpringMVC' in eligible:
        dependencies = {r['symbol'] for r in rows if r['relation'] == 'controller_dependency'}
        for start, end, name, target in annotations:
            declaration = _declaration(code, end)
            if _ANNOTATIONS[target][1] != 'route' or not declaration or declaration[0] != 'method':
                continue
            if not any(left < start < right for left, right in controllers):
                continue
            opening = code.find('{', declaration[2])
            if opening < 0 or ';' in code[declaration[2]:opening]:
                continue
            cursor, depth = opening + 1, 1
            while cursor < len(code) and depth:
                depth += (code[cursor] == '{') - (code[cursor] == '}')
                cursor += 1
            if depth:
                continue
            for call in re.finditer(r'\b(' + _IDENT + r')\s*\.\s*(' + _IDENT + r')\s*\(', code[opening:cursor]):
                if call[1] in dependencies:
                    left, right = opening + call.start(), opening + call.end()
                    emit('SpringMVC', left, right, safe_text[left:right-1].strip(),
                         'controller_method_calls', declaration[1], 'call_relation')
    # Boot entrypoint is actual invocation, not an unused imported type.
    for match in re.finditer(r"\b(" + _IDENT + r"(?:\." + _IDENT + r")*)\s*\.\s*run\s*\(\s*(" + _IDENT + r")\s*\.\s*class", code):
        if "SpringBoot" in eligible and qualify(match.group(1), {"org.springframework.boot.SpringApplication"}):
            emit("SpringBoot", match.start(), match.end(), match.group(1), "application_entrypoint", match.group(2), "call")
    # Only actual inheritance clauses can confirm MyBatisPlus contracts.
    pattern = r"\b(?:class|interface)\s+(" + _IDENT + r")\s+(?:extends|implements)\s+(" + _IDENT + r"(?:\." + _IDENT + r")*)\s*<\s*(" + _IDENT + r"(?:\." + _IDENT + r")*)(?:\s*,\s*(" + _IDENT + r"(?:\." + _IDENT + r")*))?\s*>"
    for match in re.finditer(pattern, code):
        target = qualify(match.group(2), _EXTENDS)
        if "MyBatisPlus" in eligible and target:
            emit("MyBatisPlus", match.start(), match.end(), match.group(1), _EXTENDS[target], match.group(3), "generic_inheritance")
            if target.endswith(".ServiceImpl") and match.group(4):
                emit("MyBatisPlus", match.start(), match.end(), match.group(1), "service_entity", match.group(4), "generic_inheritance")
    return sorted(rows, key=lambda row: (row["char_start"], row["relation"], row["symbol"]))


def detect(path, safe_text):
    """Signature-only qualification; deep relation extraction happens after gating."""
    source = _source(path, safe_text)
    if source is None:
        return set()
    code, qualify = source
    found = set()
    for start, end, name, target in _annotations(code, qualify):
        technology, relation = _ANNOTATIONS[target]
        declaration = _declaration(code, end)
        if declaration and (relation not in {'boot_application', 'controller', 'service',
                'component', 'repository', 'configuration', 'mapper', 'mapper_scan'}
                or declaration[0] == 'type'):
            found.add(technology)
        if relation == 'parameter_binding' and re.match(r'\s*[\w.$<>?\[\]]+\s+' + _IDENT + r'\s*[,)]', code[end:]):
            found.add(technology)
    for match in re.finditer(r'\b(' + _IDENT + r'(?:\.' + _IDENT + r')*)\.run\s*\(', code):
        if qualify(match[1], {'org.springframework.boot.SpringApplication'}):
            found.add('SpringBoot')
    for match in re.finditer(r'\b(?:extends|implements)\s+(' + _IDENT + r'(?:\.' + _IDENT + r')*)\s*<', code):
        if qualify(match[1], _EXTENDS):
            found.add('MyBatisPlus')
    return found


def analyze(path, safe_text, enabled=None):
    """Extract source-confirmed matches, filtered by the caller's eligibility gate."""
    eligible = set(TECHNOLOGIES) if enabled is None else set(enabled) & set(TECHNOLOGIES)
    return _raw_matches(path, safe_text, eligible)
