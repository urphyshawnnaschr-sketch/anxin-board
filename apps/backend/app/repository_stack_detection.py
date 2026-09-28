"""Local hints only: never execute manifests, dependencies, source or a model."""
from pathlib import PurePosixPath
DETECTOR_VERSION='repository-stack-detector/1'
EXTENSIONS={'.py':'python','.js':'javascript','.jsx':'javascript','.mjs':'javascript','.cjs':'javascript','.ts':'typescript','.tsx':'typescript','.vue':'web','.html':'web','.sql':'sql', '.java':'java','.cs':'csharp','.go':'go','.rs':'rust','.kt':'kotlin','.dart':'dart','.cpp':'cpp','.c':'c'}
MANIFESTS={'pyproject.toml':'python','requirements.txt':'python','package.json':'javascript','pom.xml':'java','build.gradle':'jvm','build.gradle.kts':'jvm','go.mod':'go','cargo.toml':'rust'}

def detect_stack(paths, adapters=()):
    extensions=dict(EXTENSIONS);manifests=dict(MANIFESTS)
    for adapter in adapters:
        for ext in adapter.extensions:extensions[ext]=adapter.language
        for name in adapter.manifests:manifests[name.casefold()]=adapter.language
    files={};clues=[]
    for path in paths:
        p=PurePosixPath(path);name=p.name.casefold();suffix=p.suffix.casefold()
        language=extensions.get(suffix,'unknown')
        manifest_language=manifests.get(name)
        if suffix=='.csproj':manifest_language='csharp'
        if manifest_language:
            clues.append({'path':path,'language_hint':manifest_language,'kind':'manifest_filename'})
            if language=='unknown':language=manifest_language
        files[path]={'language_hint':language,'extension':suffix,'is_manifest':manifest_language is not None}
    return {'detector_version':DETECTOR_VERSION,'files':files,'manifest_clues':clues,
            'languages':sorted({v['language_hint'] for v in files.values()}),
            'status':'local_filename_hints_only'}
