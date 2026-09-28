"""Synthetic process-contract checks; no repository, credential or provider access."""
import subprocess
from types import SimpleNamespace

import pytest

from app import git_client
from app.git_client import GitClient, GitClientError


@pytest.fixture
def execution(monkeypatch, tmp_path):
    client = object.__new__(GitClient)
    client.git_path = 'synthetic-git'
    client.timeout_seconds = 5
    state = SimpleNamespace(created=[], communicated=[], cleaned=[], finalized=[], timers=[], timeout=False)
    environment = {'SYNTHETIC_ENV': 'fixed'}
    monkeypatch.setattr(client, '_environment', lambda: environment)
    monkeypatch.setattr(client, '_execution_policy', lambda: ['-c', 'credential.helper='])

    class Process:
        returncode = 0
        def communicate(self, **kwargs):
            state.communicated.append(kwargs)
            if state.timeout:
                raise subprocess.TimeoutExpired('synthetic-git', kwargs['timeout'])
            return b'object bytes', b''

    process = Process()
    def popen(command, **kwargs):
        state.created.append((command, kwargs))
        return process
    class Timer:
        def __init__(self, interval, function, args):
            self.interval, self.function, self.args = interval, function, args
            self.started = False
            state.timers.append(self)
        def start(self):
            self.started = True

    monkeypatch.setattr(git_client.subprocess, 'Popen', popen)
    monkeypatch.setattr(git_client.threading, 'Timer', Timer)
    monkeypatch.setattr(git_client, '_create_kill_on_close_job', lambda proc: 42)
    monkeypatch.setattr(client, '_discard_children', lambda *args: state.cleaned.append(args))
    monkeypatch.setattr(git_client, '_finalize_job_handle', lambda *args: state.finalized.append(args))
    return client, state, process, environment, tmp_path


@pytest.mark.parametrize('explicit', [False, True])
def test_no_input_preserves_devnull_and_communicate_shape(execution, explicit):
    client, state, process, environment, cwd = execution
    options = {'input_data': None} if explicit else {}
    assert client._exec(['cat-file', '--batch'], cwd=cwd, **options) == (0, b'object bytes', b'')
    assert state.created[0][1]['stdin'] == subprocess.DEVNULL
    assert set(state.communicated[0]) == {'timeout'}
    assert 0 < state.communicated[0]['timeout'] <= client.timeout_seconds


@pytest.mark.parametrize('payload', [b'', b'abc\n', b'x' * (64 * 1024)], ids=['empty', 'small', '64k'])
def test_bounded_bytes_use_pipe_and_existing_execution_envelope(execution, payload):
    client, state, process, environment, cwd = execution
    assert client._exec(['cat-file', '--batch'], cwd=cwd, input_data=payload) == (0, b'object bytes', b'')
    command, options = state.created[0]
    assert command == ['synthetic-git', '-c', 'credential.helper=', 'cat-file', '--batch']
    assert options['stdin'] == subprocess.PIPE
    assert options['stdout'] == options['stderr'] == subprocess.PIPE
    assert options['shell'] is False
    assert options['env'] is environment
    assert options['cwd'] == str(cwd)
    assert state.communicated[0]['input'] is payload
    assert set(state.communicated[0]) == {'input', 'timeout'}
    assert 0 < state.communicated[0]['timeout'] <= client.timeout_seconds
    timer = state.timers[0]
    assert timer.started and timer.daemon
    assert timer.function is git_client._timeout_handler
    assert timer.args[1:3] == [42, process]
    assert state.finalized == [(42, timer, timer.args[3])]


class DeceptiveBytes(bytes):
    def __len__(self):
        return 0


@pytest.mark.parametrize('payload', ['secret text', bytearray(b'x'), memoryview(b'x'), True, 1, [], {}, b'x' * (64 * 1024 + 1), DeceptiveBytes(b'x' * (64 * 1024 + 1))], ids=['text', 'bytearray', 'memoryview', 'bool', 'int', 'list', 'dict', 'over64k', 'deceptive-subclass'])
def test_invalid_input_rejected_before_process_creation(execution, payload):
    client, state, _, _, cwd = execution
    with pytest.raises(GitClientError) as raised:
        client._exec(['cat-file', '--batch'], cwd=cwd, input_data=payload)
    assert raised.value.code == 'GIT_CHECK_FAILED'
    assert not state.created and not state.timers and not state.communicated
    assert 'secret text' not in str(raised.value)


@pytest.mark.parametrize('payload', [None, b'abc\n'])
def test_timeout_with_or_without_input_keeps_tree_cleanup_and_job_finalization(execution, payload):
    client, state, process, _, cwd = execution
    state.timeout = True
    with pytest.raises(GitClientError) as raised:
        client._exec(['cat-file', '--batch'], cwd=cwd, input_data=payload)
    assert raised.value.code == 'GIT_COMMAND_TIMEOUT'
    timer = state.timers[0]
    assert timer.args[0].is_set()
    assert state.cleaned == [(process, 42, timer.args[3])]
    assert state.finalized == [(42, timer, timer.args[3])]

@pytest.mark.parametrize('payload', [b'', b'line-one\nline-two\n', b'x' * (64 * 1024)], ids=['empty', 'lines', '64k'])
def test_real_local_process_receives_exact_bytes(tmp_path, monkeypatch, payload):
    import os
    import sys
    client = object.__new__(GitClient)
    client.git_path, client.timeout_seconds = sys.executable, 5
    monkeypatch.setattr(client, '_environment', lambda: {'SystemRoot': os.environ.get('SystemRoot', '')})
    monkeypatch.setattr(client, '_execution_policy', lambda: [])
    result = client._exec(['-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], cwd=tmp_path, input_data=payload)
    assert result == (0, payload, b'')


def test_real_input_timeout_allows_followup_process(tmp_path, monkeypatch):
    import os
    import sys
    import time
    client = object.__new__(GitClient)
    client.git_path, client.timeout_seconds = sys.executable, 1
    monkeypatch.setattr(client, '_environment', lambda: {'SystemRoot': os.environ.get('SystemRoot', '')})
    monkeypatch.setattr(client, '_execution_policy', lambda: [])
    started = time.monotonic()
    with pytest.raises(GitClientError) as raised:
        client._exec(['-c', 'import time; time.sleep(30)'], cwd=tmp_path, input_data=b'x' * (64 * 1024))
    assert raised.value.code == 'GIT_COMMAND_TIMEOUT'
    assert time.monotonic() - started < 5
    assert client._exec(['-c', 'print("ok")'], cwd=tmp_path)[1].strip() == b'ok'
