from pathlib import Path
import pytest
from app.git_client import GitClient, GitClientError

@pytest.mark.parametrize("stderr", [
    b"fatal: Cannot prompt because user interactivity has been disabled.",
    b"fatal: unable to get password from user",
])
def test_background_fetch_reports_auth_without_enabling_prompts(monkeypatch, stderr):
    client = GitClient()
    monkeypatch.setattr(client, "_exec", lambda *a, **k: (128, b"", stderr))
    with pytest.raises(GitClientError) as caught:
        client._run(["fetch", "origin"], cwd=Path.cwd())
    assert caught.value.code == "GIT_AUTH_FAILED"
    assert client._environment()["GCM_INTERACTIVE"] == "Never"
    assert "credential.interactive=false" in client._execution_policy()

from app.git_client import _assert_safe_local_config

@pytest.mark.parametrize('scope,key,value,accepted', [
 ('https://github.com/example/project','username','example-user',True),
 ('https://github.com','username','example-user',False),
 ('https://github.com/example/other','username','example-user',False),
 ('https://github.com/example/project','helper','manager',False),
 ('https://github.com/example/project','password','synthetic',False),
 ('https://github.com/example/project','username','bad/user',False),
])
def test_only_exact_repo_account_selection_is_admitted(tmp_path,scope,key,value,accepted):
    (tmp_path/'.git').mkdir()
    url='https://github.com/example/project'
    (tmp_path/'.git/config').write_text(f'[remote "origin"]\n url = {url}\n[credential "{scope}"]\n {key} = {value}\n',encoding='utf-8')
    if accepted:
        assert _assert_safe_local_config(tmp_path,url)==tmp_path/'.git/config'
    else:
        with pytest.raises(GitClientError): _assert_safe_local_config(tmp_path,url)
