"""Conservative structural adapters for Repository Atlas recall.

These parsers expose only literal identifiers already present in safe source. They are
retrieval hints, never semantic proof that a PRD requirement is implemented.
"""
import re
from app.repository_evidence import ParseResult


def text_terms(text):
    expanded=re.sub(r'([a-z])([A-Z])',r'\1 \2',text)
    words=set(re.findall(r'[^\W_]+',expanded.casefold(),flags=re.UNICODE))
    for sequence in re.findall(r'[\u4e00-\u9fff]+',expanded):
        words.update(sequence[i:i+2] for i in range(len(sequence)-1))
    return sorted(words)

class GenericFallback:
    adapter_id='generic-text/1';language='unknown';extensions=frozenset();manifests=frozenset()
    def parse(self,path,safe_text):return ParseResult(self.adapter_id,'fallback',{})

class PythonHeuristics:
    adapter_id='python-heuristics/1';language='python'
    extensions=frozenset({'.py'});manifests=frozenset()
    def parse(self,path,safe_text):
        patterns={'symbol':r'(?m)^\s*(?:async\s+)?(?:def|class)\s+([\w$]+)',
         'route':r'''@\w+\.(?:get|post|put|patch|delete)\(\s*["']([^"']+)["']''',
         'table':r'(?i)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w]+)',
         'test':r'(?m)^\s*(?:async\s+)?def\s+(test_[\w]+)'}
        structured={k:sorted(set(re.findall(p,safe_text))) for k,p in patterns.items()}
        imports=set(re.findall(r'(?m)^\s*from\s+([.\w]+)\s+import\b',safe_text))
        imports.update(re.findall(r'(?m)^\s*import\s+([.\w]+)',safe_text))
        structured['import']=sorted(value for value in imports if value and value in safe_text)
        return ParseResult(self.adapter_id,'heuristic',structured)

class JavaSpringHeuristics:
    """Extract literal Java/Spring structure without synthesizing combined routes."""
    adapter_id='java-spring-heuristics/1';language='java'
    extensions=frozenset({'.java'});manifests=frozenset()
    _TYPE=re.compile(r'(?m)^\s*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)')
    _METHOD=re.compile(r'(?m)^\s*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?(?:<[^>]+>\s+)?[\w$<>,.?\[\]\s]+\s+([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{')
    _IMPORT=re.compile(r'(?m)^\s*import\s+(?:static\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*;')
    _MAPPING=re.compile(r'''@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\s*\(\s*(?:(?:value|path)\s*=\s*)?["']([^"']+)["']''')
    _TABLE=re.compile(r'''@(?:TableName|Table)\s*\(\s*(?:name\s*=\s*)?["']([^"']+)["']''')
    def parse(self,path,safe_text):
        symbols=set(self._TYPE.findall(safe_text));symbols.update(self._METHOD.findall(safe_text))
        routes={match.group(2) for match in self._MAPPING.finditer(safe_text)}
        imports={value for value in self._IMPORT.findall(safe_text) if value in safe_text}
        tables={value for value in self._TABLE.findall(safe_text) if value in safe_text}
        structured={
            'symbol':sorted(value for value in symbols if value in safe_text),
            'route':sorted(value for value in routes if value in safe_text),
            'table':sorted(tables),
            'import':sorted(imports),
        }
        return ParseResult(self.adapter_id,'heuristic',structured)

class WebHeuristics:
    adapter_id='js-ts-web-heuristics/2';language='javascript-web'
    extensions=frozenset({'.js','.jsx','.mjs','.cjs','.ts','.tsx','.vue','.html','.sql'});manifests=frozenset()
    _REQUEST_OBJECT=re.compile(r'''\brequest\s*\(\s*\{(?P<body>.{0,2400}?)\}\s*\)''',re.DOTALL)
    _URL_FIELD=re.compile(r'''\burl\s*:\s*["'`]([^"'`]+)["'`]''')
    @staticmethod
    def _static_endpoint(value):
        return bool(value) and '${' not in value and value in value
    def parse(self,path,safe_text):
        patterns={'symbol':r'(?m)^\s*(?:export\s+)?(?:async\s+)?(?:class|function|interface|type)\s+([\w$]+)',
         'route':r'''(?:app|router)\.(?:get|post|put|patch|delete)\(\s*["']([^"']+)["']''',
         'table':r'(?i)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w]+)',
         'api':r'''(?:fetch|axios\.(?:get|post|put|delete|patch))\(\s*["'`]([^"'`]+)["'`]'''}
        structured={k:sorted(set(re.findall(p,safe_text))) for k,p in patterns.items()}
        structured['api']=[value for value in structured['api'] if self._static_endpoint(value)]
        request_urls=set()
        for match in self._REQUEST_OBJECT.finditer(safe_text):
            url=self._URL_FIELD.search(match.group('body'))
            if url and self._static_endpoint(url.group(1)) and url.group(1) in safe_text:
                request_urls.add(url.group(1))
        structured['api']=sorted(set(structured['api'])|request_urls)
        imports=set(re.findall(r'''(?:from\s*|require\(\s*|import\(\s*)["'`]([^"'`]+)["'`]''',safe_text))
        structured['import']=sorted(value for value in imports if value and value in safe_text)
        return ParseResult(self.adapter_id,'heuristic',structured)

DEFAULT_ADAPTERS=(PythonHeuristics(),JavaSpringHeuristics(),WebHeuristics())
