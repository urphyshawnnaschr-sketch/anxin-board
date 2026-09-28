"""Conservative Vue-family source evidence. Pure functions over already-redacted text.

This is bounded lexical evidence, not a JavaScript interpreter or full parser.
Root owns manifest scope, redaction, source eligibility and global offsets.
"""
import re
import posixpath

TECHNOLOGIES = {
    'Vue2': ('vue',), 'VueRouter': ('vue-router',), 'Vuex': ('vuex',),
    'Axios': ('axios',), 'ElementUI': ('element-ui',),
}
_IDS = {'Vue2': 'Vue2SfcAdapter', 'VueRouter': 'VueRouterAdapter',
        'Vuex': 'VuexAdapter', 'Axios': 'AxiosAdapter', 'ElementUI': 'ElementUIAdapter'}
_IDENT = r'[A-Za-z_$][\w$]*'
_TOKEN = re.compile(r'<!--.*?-->|/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`', re.S)
_IMPORT = re.compile(r'\bimport\s+(?P<binding>[^;\n]+?)\s+from\s*[\'"](?P<module>[^\'"\n]+)[\'"]')


def _code(text):
    return _TOKEN.sub(lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), text)


def _imports(text, code):
    result = []
    for m in _IMPORT.finditer(text):
        if not code[m.start():m.start() + 6].strip():
            continue
        binding = m['binding'].strip()
        aliases = []
        if re.fullmatch(_IDENT, binding):
            aliases = [binding]
        elif binding.startswith('* as '):
            aliases = [binding[5:].strip()]
        elif binding.startswith('{') and binding.endswith('}'):
            for item in binding[1:-1].split(','):
                alias = item.strip().split(' as ')[-1].strip()
                if re.fullmatch(_IDENT, alias):
                    aliases.append(alias)
        aliases = [alias for alias in aliases if not _shadowed(alias, code)]
        result.append((m, m['module'], aliases))
    return result


def _used(alias, code, pattern):
    return re.search(pattern.format(a=re.escape(alias)), code)


def _shadowed(alias, code):
    # Conservatively reject the whole file when lexical scope resolution would
    # be needed. A missed usage is UNKNOWN; an imported name is not proof of it.
    if re.search(r'\b(?:const|let|var|class|function)\s+' + re.escape(alias) + r'\b', code):
        return True
    for match in re.finditer(r'\b(?:const|let|var)\s*([\[{][^;=]*[\]}])\s*=', code):
        if re.search(r'\b' + re.escape(alias) + r'\b', match[1]):
            return True
    for match in re.finditer(r'\(([^()]*)\)\s*(?::[^;={]+)?\s*(?:=>|\{)', code):
        if re.search(r'\b' + re.escape(alias) + r'\b', match[1]):
            return True
    return bool(re.search(r'\b' + re.escape(alias) + r'\s*=>', code))


def detect(path, safe_text):
    """Only imported dependencies with executable usage qualify locally."""
    if not str(path).lower().endswith(('.js', '.ts', '.vue', '.jsx', '.tsx')):
        return set()
    code = _code(safe_text)
    found = set()
    for _, module, aliases in _imports(safe_text, code):
        for alias in aliases:
            if module == 'vue' and _used(alias, code, r'\bnew\s+{a}\s*\(|\b{a}\.prototype\.'):
                found.add('Vue2')
            if module == 'vue-router' and _used(alias, code, r'\bnew\s+{a}\s*\(|\b{a}\.createRouter\s*\('):
                found.add('VueRouter')
            if module == 'vuex' and _used(alias, code, r'\bnew\s+{a}\.Store\s*\(|\b{a}\.createStore\s*\('):
                found.add('Vuex')
            if module == 'axios' and _used(alias, code, r'\b{a}\s*(?:\(|\.(?:create|get|post|put|patch|delete|request|head|options)\s*\()'):
                found.add('Axios')
            if module == 'element-ui' and (_used(alias, code, r'\.use\s*\(\s*{a}\s*[,)]') or _used(alias, code, r'\b{a}\s*(?:\(|\.(?:success|warning|error|info|confirm|alert)\s*\()')):
                found.add('ElementUI')
    return found


def scope_eligible(path, safe_text):
    """SFC syntax may inherit Vue2 only from root-confirmed manifest scope."""
    code = _code(safe_text)
    if str(path).lower().endswith('.vue') and re.search(r'<template\b[^>]*>[\s\S]*?</template\s*>', code) and re.search(r'<script\b[^>]*>[\s\S]*?</script\s*>', code):
        return {'Vue2'}
    return set()


def _resolve_local(importer, module, sources):
    """Resolve only supplied same-scope sources; never probe the filesystem."""
    importer = str(importer).replace('\\', '/')
    if module.startswith('.'):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
    elif module.startswith('@/'):
        prefix, sep, _ = importer.rpartition('/src/')
        if sep:
            base = prefix + '/src/' + module[2:]
        elif importer.startswith('src/'):
            base = 'src/' + module[2:]
        else:
            return None
    else:
        return None
    if base.startswith('../') or base.startswith('/'):
        return None
    candidates = [base, base+'.js', base+'.ts', base+'.vue', base+'/index.js', base+'/index.ts']
    matched = [candidate for candidate in candidates if candidate in sources]
    return matched[0] if len(matched) == 1 else None


def context_eligible(path, safe_text, sources):
    """One-hop import lineage inside the root-supplied manifest scope only.

    Router registration may point to a route-definition module. HTTP wrappers
    must import and call a client whose defining source actually uses Axios.
    """
    path = str(path).replace('\\', '/')
    code = _code(safe_text)
    result = set()
    for imported, module, aliases in _imports(safe_text, code):
        target = _resolve_local(path, module, sources)
        if target and _exports_axios_client(sources[target]):
            if any(imported['binding'].strip() == alias and _used(alias, code, r'\b{a}\s*(?:\(|\.(?:get|post|put|patch|delete|request|head|options)\s*\()') for alias in aliases):
                result.add('Axios')
    if _default_route_collection(code) is not None:
        for importer, text in sources.items():
            if 'VueRouter' not in detect(importer, text):
                continue
            importer_code = _code(text)
            for imported, module, aliases in _imports(text, importer_code):
                if _resolve_local(importer, module, sources) != path:
                    continue
                if any(imported['binding'].strip() == alias and _used(alias, importer_code, r'\broutes\s*:\s*{a}\b') for alias in aliases):
                    result.add('VueRouter')
    return result


def _default_route_collection(code):
    """Resolve a literal default route array or a const holding that array."""
    export = re.search(r'\bexport\s+default\s*', code)
    if not export:
        return None
    start = export.end()
    if code[start:start+1] != '[':
        alias = re.match('(' + _IDENT + r')\s*(?:;|$)', code[start:])
        if not alias:
            return None
        declarations = list(re.finditer(r'\bconst\s+' + re.escape(alias[1]) + r'\s*=\s*\[', code))
        if len(declarations) != 1:
            return None
        start = declarations[0].end() - 1
    depth, end = 1, start + 1
    while end < len(code) and depth:
        depth += (code[end] == '[') - (code[end] == ']')
        end += 1
    tail = code[end:]
    # ASI before a new declaration is unambiguous; a newline before .map(),
    # indexing or a call still continues the expression and must be rejected.
    terminated = re.match(r'\s*(?:;|$)', tail) or re.match(
        r'\s*\n\s*(?:export|const|let|var|function|class)\b', tail)
    if (depth or not terminated
            or not re.search(r'\bcomponent\s*:', code[start:end])
            or not re.search(r'\bpath\s*:', code[start:end])):
        return None
    return start, end


def _exports_axios_client(text):
    code = _code(text)
    for _, module, aliases in _imports(text, code):
        if module != 'axios':
            continue
        for alias in aliases:
            if re.search(r'\bexport\s+default\s+' + re.escape(alias) + r'\s*\.create\s*\(', code):
                return True
            for match in re.finditer(r'\b(?:const|let|var)\s+(' + _IDENT + r')\s*=\s*' + re.escape(alias) + r'\.create\s*\(', code):
                if re.search(r'\bexport\s+default\s+' + re.escape(match[1]) + r'\s*(?:;|$)', code):
                    return True
    return False


def analyze_context(path, safe_text, sources, enabled):
    return analyze(path, safe_text, enabled, source_context=sources)


def analyze(path, safe_text, enabled=None, *, source_context=None):
    """Emit local character spans; enabled is the root's source-confirmed scope."""
    if not str(path).lower().endswith(('.js', '.ts', '.vue', '.jsx', '.tsx')):
        return []
    active = detect(path, safe_text) if enabled is None else set(enabled) & set(TECHNOLOGIES)
    code = _code(safe_text)
    imports = _imports(safe_text, code)
    rows = []

    def emit(tech, kind, match, symbol=None, relation='declares', related=None, related_path=None):
        symbol = symbol if symbol is not None else match[0]
        # Never return address literals, interpolated expressions, or arbitrary text.
        if not symbol or len(symbol) > 160 or re.search(r'https?://|(?:\d{1,3}\.){3}\d{1,3}|[\r\n]', symbol):
            return
        row = dict(technology=tech, adapter_id=_IDS[tech], adapter_version='1',
                   evidence_type=kind, symbol=symbol, relation=relation,
                   char_start=match.start(), char_end=match.end())
        if related:
            row['related_symbol'] = related
        if related_path and re.fullmatch(r'(?:\./|\.\./|@/)[\w./@-]+', related_path):
            row['related_path'] = related_path
        rows.append(row)

    for tech in sorted(active):
        if tech == 'Vue2':
            for m in re.finditer(r'<(template|script)\b[^>]*>', code):
                emit(tech, 'sfc_section', m, m[1], 'contains')
            for m, module, aliases in imports:
                if not module.endswith('.vue'):
                    continue
                for alias in aliases:
                    kebab = re.sub(r'(?<!^)([A-Z])', r'-\1', alias).lower()
                    usage = re.search(r'<\s*(' + re.escape(alias) + '|' + re.escape(kebab) + r')(?=[\s/>])', code)
                    registration = re.search(r'\bcomponents\s*:\s*\{[^}]*\b' + re.escape(alias) + r'\b', code)
                    if usage or registration:
                        emit(tech, 'component_import', m, alias, 'uses_component', related_path=module)
            for m, module, aliases in imports:
                if module == 'vue':
                    for alias in aliases:
                        for usage in re.finditer(r'\bnew\s+' + re.escape(alias) + r'\s*\(', code):
                            emit(tech, 'bootstrap', usage, alias, 'constructs')
        elif tech == 'VueRouter':
            for _, module, aliases in imports:
                if module == 'vue-router':
                    for alias in aliases:
                        for m in re.finditer(r'\bnew\s+' + re.escape(alias) + r'\s*\(', code):
                            emit(tech, 'router_registration', m, alias, 'constructs')
            for imported, module, aliases in imports:
                for alias in aliases:
                    if _used(alias, code, r'\broutes\s*:\s*{a}\b'):
                        emit(tech, 'route_collection_import', imported, alias, 'references', related_path=module)
            for m in re.finditer(r'\broutes\s*:\s*(' + _IDENT + r')\b', code):
                emit(tech, 'route_collection', m, m[1], 'references')
            if not re.search(r'\bcomponent\s*:', code) and tech not in detect(path, safe_text):
                continue
            for m in re.finditer(r'\b(path|name)\s*:\s*([\'"])([^\'"\n]+)\2', safe_text):
                if code[m.start():m.start()+len(m[1])].strip() and re.fullmatch(r'[/\w:@.*-]+', m[3]):
                    emit(tech, 'route_' + m[1], m, m[3])
            for m in re.finditer(r'\bcomponent\s*:\s*(' + _IDENT + r')\b', code):
                if m[1] != 'import':
                    emit(tech, 'route_component', m, m[1], 'uses_component')
            for m in re.finditer(r'\bimport\s*\(\s*([\'"])((?:\./|\.\./|@/)[\w./@-]+)\1\s*\)', safe_text):
                if code[m.start():m.start()+6].strip():
                    emit(tech, 'lazy_component', m, 'import', 'loads_component', related_path=m[2])
            for left, right in _objects(code):
                paths = [m for m in re.compile(r'\bpath\s*:\s*([\'"])([/\w:@.*-]+)\1').finditer(safe_text, left, right)
                         if _immediate(code, left, m.start())]
                components = [m for m in re.compile(r'\bcomponent\s*:').finditer(code, left, right)
                              if _immediate(code, left, m.start())]
                if len(paths) != 1 or len(components) != 1:
                    continue
                component = components[0]
                tail = safe_text[component.end():right]
                lazy = re.match(r'\s*\([^)]*\)\s*=>\s*import\s*\(\s*([\'"])((?:\./|\.\./|@/)[\w./@-]+)\1', tail)
                binding = re.match(r'\s*(' + _IDENT + r')\b', tail)
                if lazy:
                    emit(tech, 'route_binding', paths[0], paths[0][2], 'loads_component', related_path=lazy[2])
                elif binding:
                    emit(tech, 'route_binding', paths[0], paths[0][2], 'uses_component', related=binding[1])
        elif tech == 'Vuex':
            for m in re.finditer(r'\b(modules|state|actions|mutations|getters)\s*:', code):
                emit(tech, 'store_section', m, m[1], 'contains')
                opening = re.match(r'\s*\{', code[m.end():])
                if opening:
                    start = m.end() + opening.end()
                    depth = 1
                    end = start
                    while end < len(code) and depth:
                        depth += (code[end] == '{') - (code[end] == '}')
                        end += 1
                    if depth == 0:
                        for member in re.finditer(r'\b(' + _IDENT + r')\s*(?=[:(])', code[start:end-1]):
                            before = code[start:start + member.start()]
                            if not re.search(r'(?:^|[,}])\s*$', before) or before.count('{') != before.count('}'):
                                continue
                            # Re-run over a bounded range to retain original offsets.
                            for candidate in re.compile(re.escape(member[1])).finditer(code, start + member.start(), start + member.end()):
                                emit(tech, 'store_member', candidate, member[1], 'belongs_to', related=m[1])
                                break
            for m in re.finditer(r'\b(commit|dispatch)\s*\(\s*([\'"])([\w/-]+)\2', safe_text):
                if code[m.start():m.start()+len(m[1])].strip():
                    emit(tech, 'store_call', m, m[1], 'references', related=m[3])
        elif tech == 'Axios':
            aliases = [a for _, module, names in imports if module == 'axios' for a in names]
            clients = list(aliases)
            for alias in aliases:
                for m in re.finditer(r'\b(?:const|let|var)\s+(' + _IDENT + r')\s*=\s*' + re.escape(alias) + r'\.create\s*\(', code):
                    clients.append(m[1]); emit(tech, 'http_instance', m, m[1], 'creates_client', related=alias)
            # Locally imported wrappers are evidence only when the root enabled Axios.
            for m, module, names in imports:
                target = _resolve_local(path, module, source_context or {})
                if target and _exports_axios_client(source_context[target]):
                    for name in names:
                        if m['binding'].strip() == name and _used(name, code, r'\b{a}\s*(?:\(|\.(?:get|post|put|patch|delete|request|head|options)\s*\()'):
                            clients.append(name)
                            emit(tech, 'http_wrapper_import', m, name, 'uses_client', related_path=module)
            for client in set(clients):
                for m in re.finditer(r'\b' + re.escape(client) + r'(?:\.(get|post|put|patch|delete|request|head|options))?\s*\(', code):
                    emit(tech, 'http_call', m, client, 'calls', related=m[1])
                    config = re.match(r'\s*\{', code[m.end():])
                    if config:
                        start = m.end() + config.end() - 1
                        bound = next(((l, r) for l, r in _objects(code) if l == start), None)
                        if bound:
                            for method in re.compile(r'\bmethod\s*:\s*([\'"])(get|post|put|patch|delete|head|options)\1', re.I).finditer(safe_text, *bound):
                                if _immediate(code, bound[0], method.start()):
                                    emit(tech, 'request_method', method, method[2], 'request_config_for', related=client)
            if clients:
                for m in re.finditer(r'\burl\s*:\s*(' + _IDENT + r')\b', code):
                    emit(tech, 'symbolic_endpoint', m, m[1], 'references')
        elif tech == 'ElementUI':
            for m in re.finditer(r'<(el-[a-z][a-z0-9-]*)(?=[\s/>])', code):
                emit(tech, 'ui_component', m, m[1], 'uses_component')
            for m, module, aliases in imports:
                if module == 'element-ui':
                    for alias in aliases:
                        if _used(alias, code, r'\.use\s*\(\s*{a}\s*[,)]') or _used(alias, code, r'\b{a}\s*(?:\(|\.(?:success|warning|error|info|confirm|alert)\s*\()'):
                            emit(tech, 'ui_import', m, alias, 'uses')
    return sorted(rows, key=lambda row: (row['char_start'], row['technology'], row['evidence_type']))


def _objects(code):
    stack = []
    result = []
    for i, char in enumerate(code):
        if char == '{': stack.append(i)
        elif char == '}' and stack: result.append((stack.pop(), i + 1))
    return result


def _immediate(code, left, position):
    prefix = code[left+1:position]
    return prefix.count('{') == prefix.count('}') and prefix.count('[') == prefix.count(']')
