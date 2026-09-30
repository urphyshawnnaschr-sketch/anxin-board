"""Synthetic local Windows pipe tests: no application state or credentials."""
import ctypes
from ctypes import wintypes as w
import os
import time

import pytest
from app import local_session_handoff as handoff

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")


def api():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in [
        ("CreateFileW", [w.LPCWSTR,w.DWORD,w.DWORD,w.LPVOID,w.DWORD,w.DWORD,w.HANDLE],w.HANDLE),
        ("CloseHandle",[w.HANDLE],w.BOOL),
        ("ReadFile",[w.HANDLE,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID],w.BOOL),
        ("WriteFile",[w.HANDLE,w.LPCVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID],w.BOOL),
        ("SetNamedPipeHandleState",[w.HANDLE,ctypes.POINTER(w.DWORD),w.LPVOID,w.LPVOID],w.BOOL),
        ("GetNamedPipeServerProcessId",[w.HANDLE,ctypes.POINTER(w.ULONG)],w.BOOL),
    ]:
        fn=getattr(k,name);fn.argtypes=args;fn.restype=result
    return k


def connect(server):
    k=api();deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        h=k.CreateFileW(server.pipe_path,0xC0000000,0,None,3,0,None)
        if h != ctypes.c_void_p(-1).value:
            mode=w.DWORD(1)
            assert k.SetNamedPipeHandleState(h,ctypes.byref(mode),None,None)
            return k,h
        time.sleep(.01)
    pytest.fail("synthetic pipe connect timeout")


def send(k,h,raw):
    count=w.DWORD()
    assert k.WriteFile(h,raw,len(raw),ctypes.byref(count),None)
    assert count.value==len(raw)


def read(k,h):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        buf=ctypes.create_string_buffer(512);count=w.DWORD()
        if k.ReadFile(h,buf,len(buf),ctypes.byref(count),None):
            if count.value:return buf.raw[:count.value]
        elif ctypes.get_last_error() not in (232,536):
            return b""
        time.sleep(.01)
    return b""


def test_real_pipe_success_bound_to_runtime_pid_and_stop():
    calls=[]
    server=handoff.start_handoff_server(lambda:calls.append(1) or "synthetic-token")
    try:
        assert server.pipe_name==f"AnxinBoard.LocalSession.{os.getpid()}"
        k,h=connect(server)
        try:
            pid=w.ULONG();assert k.GetNamedPipeServerProcessId(h,ctypes.byref(pid))
            assert pid.value==os.getpid()
            send(k,h,b"MINT\n")
            assert read(k,h)==b"synthetic-token\n"
            assert calls==[1]
        finally:k.CloseHandle(h)
    finally:server.stop()
    assert not server._thread.is_alive()
    server.stop()
    assert k.CreateFileW(server.pipe_path,0xC0000000,0,None,3,0,None)==ctypes.c_void_p(-1).value


@pytest.mark.parametrize("command",[b"BAD\n",b"MINT\nEXTRA",b"x"*32,b"MINT\r\n"])
def test_bad_command_never_mints(command):
    calls=[];server=handoff.start_handoff_server(lambda:calls.append(1) or "synthetic")
    try:
        k,h=connect(server)
        try:
            send(k,h,command);assert read(k,h)==b"";assert calls==[]
        finally:k.CloseHandle(h)
    finally:server.stop()


def test_stalled_client_times_out_and_next_connection_works(monkeypatch):
    monkeypatch.setattr(handoff,"_IO_TIMEOUT",.12)
    calls=[];server=handoff.start_handoff_server(lambda:calls.append(1) or "synthetic")
    try:
        k,h=connect(server)
        try:send(k,h,b"MI");assert read(k,h)==b"";assert calls==[]
        finally:k.CloseHandle(h)
        k,h=connect(server)
        try:send(k,h,b"MINT\n");assert read(k,h)==b"synthetic\n"
        finally:k.CloseHandle(h)
    finally:server.stop()


def test_preexisting_instance_fails_closed_and_original_keeps_ownership():
    first=handoff.start_handoff_server(lambda:"first")
    try:
        with pytest.raises(handoff.HandoffError,match="HANDOFF_PIPE_UNAVAILABLE"):
            handoff.start_handoff_server(lambda:"second")
        k,h=connect(first)
        try:send(k,h,b"MINT\n");assert read(k,h)==b"first\n"
        finally:k.CloseHandle(h)
    finally:first.stop()


@pytest.mark.parametrize("mint",[lambda:"bad\nvalue",lambda:"秘密",lambda:"x"*257,lambda:1,lambda:(_ for _ in ()).throw(ValueError("private"))])
def test_invalid_mint_or_callback_error_returns_no_data(mint):
    server=handoff.start_handoff_server(mint)
    try:
        k,h=connect(server)
        try:send(k,h,b"MINT\n");assert read(k,h)==b""
        finally:k.CloseHandle(h)
    finally:server.stop()


def test_stop_interrupts_stalled_read():
    server=handoff.start_handoff_server(lambda:"synthetic")
    k,h=connect(server)
    try:
        send(k,h,b"M");start=time.monotonic();server.stop()
        assert time.monotonic()-start<1
        assert not server._thread.is_alive()
    finally:k.CloseHandle(h);server.stop()


def _canonical_dacl_sddl(value):
    """Use Windows' SID spelling while preserving the complete DACL contract."""
    native=handoff._WinApi()
    convert=native.a.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes=[w.LPVOID,w.DWORD,w.DWORD,ctypes.POINTER(w.LPWSTR),w.LPVOID]
    convert.restype=w.BOOL
    descriptor=w.LPVOID();text=w.LPWSTR()
    try:
        assert native.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(value,1,ctypes.byref(descriptor),None)
        assert convert(descriptor,1,4,ctypes.byref(text),None)
        return text.value
    finally:
        if text:native.k.LocalFree(ctypes.cast(text,w.LPVOID))
        if descriptor:native.k.LocalFree(descriptor)


def test_acl_canonicalization_accepts_exact_local_administrator_sid_alias():
    # Hosted Windows may serialize the current RID-500 SID as LA. Obtain the
    # numeric form natively so this regression also runs under ordinary users.
    native=handoff._WinApi()
    native.a.ConvertStringSidToSidW.argtypes=[w.LPCWSTR,ctypes.POINTER(w.LPVOID)]
    native.a.ConvertStringSidToSidW.restype=w.BOOL
    sid=w.LPVOID();numeric=w.LPWSTR()
    try:
        assert native.a.ConvertStringSidToSidW('LA',ctypes.byref(sid))
        assert native.a.ConvertSidToStringSidW(sid,ctypes.byref(numeric))
        assert _canonical_dacl_sddl('D:P(A;;FA;;;'+numeric.value+')')=='D:P(A;;FA;;;LA)'
    finally:
        if numeric:native.k.LocalFree(ctypes.cast(numeric,w.LPVOID))
        if sid:native.k.LocalFree(sid)


@pytest.mark.parametrize('different',[
    'D:(A;;FA;;;LA)',  # inherited permissions are no longer blocked
    'D:P(A;;FA;;;LA)(A;;FA;;;BA)',  # an additional trustee
    'D:P(A;;FR;;;LA)',  # a different access mask
    'D:P(A;;FA;;;BA)',  # Administrators group is not the current user's SID
])
def test_acl_canonicalization_does_not_weaken_descriptor_equality(different):
    assert _canonical_dacl_sddl(different)!=_canonical_dacl_sddl('D:P(A;;FA;;;LA)')


def test_actual_pipe_acl_is_protected_current_user_only():
    server=handoff.start_handoff_server(lambda:"synthetic")
    a=ctypes.WinDLL("advapi32",use_last_error=True)
    a.GetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,w.LPVOID,w.LPVOID,w.LPVOID,w.LPVOID,ctypes.POINTER(w.LPVOID)]
    a.GetSecurityInfo.restype=w.DWORD
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[w.LPVOID,w.DWORD,w.DWORD,ctypes.POINTER(w.LPWSTR),w.LPVOID]
    a.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype=w.BOOL
    descriptor=w.LPVOID();text=w.LPWSTR()
    try:
        assert a.GetSecurityInfo(server._handle,6,4,None,None,None,None,ctypes.byref(descriptor))==0
        assert a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor,1,4,ctypes.byref(text),None)
        expected=_canonical_dacl_sddl("D:P(A;;FA;;;"+server._api.current_sid()+")")
        assert text.value==expected
    finally:
        if text:server._api.k.LocalFree(ctypes.cast(text,w.LPVOID))
        if descriptor:server._api.k.LocalFree(descriptor)
        server.stop()


def test_stop_after_response_without_client_read_is_bounded():
    minted=[];server=handoff.start_handoff_server(lambda:minted.append(True) or "synthetic")
    k,h=connect(server)
    try:
        send(k,h,b"MINT\n")
        deadline=time.monotonic()+1
        while not minted and time.monotonic()<deadline:time.sleep(.01)
        assert minted
        start=time.monotonic();server.stop()
        assert time.monotonic()-start<1
        assert not server._thread.is_alive()
    finally:k.CloseHandle(h);server.stop()


def test_non_windows_does_not_start_or_mint(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(handoff,"os",SimpleNamespace(name="posix"))
    assert handoff.start_handoff_server(lambda:pytest.fail("must not mint")) is None


@pytest.mark.parametrize("case",["success","invalid_token","wrong_server_pid"])
def test_real_windows_powershell5_launcher_function_against_synthetic_pipe(case,monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    import subprocess
    source=(Path(__file__).resolve().parents[2]/"installer/windows/start-installed-product.ps1").read_text(encoding="utf-8-sig")
    function="function Request-LocalSessionHandoff"+source.split("function Request-LocalSessionHandoff",1)[1].split("function Get-AvailableLoopbackPort",1)[0]
    pid=os.getpid()
    if case=="wrong_server_pid":
        pid+=1000000
        monkeypatch.setattr(handoff,"os",SimpleNamespace(name="nt",getpid=lambda:pid))
    calls=[]
    server=handoff.start_handoff_server(lambda:calls.append(True) or ("s"*43 if case!="invalid_token" else "short"))
    try:
        check=("if ($value -ne ('s' * 43)) { exit 12 }; [Console]::WriteLine('PASS')" if case=="success" else "exit 13")
        caught=("exit 14" if case=="success" else "[Console]::WriteLine('PASS')")
        script="$ErrorActionPreference='Stop'\n"+function+f"\ntry {{ $value=Request-LocalSessionHandoff -RuntimePid {pid}; {check} }} catch {{ {caught} }}\n"
        result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-Command",script],capture_output=True,text=True,timeout=15)
        assert result.returncode==0
        assert result.stdout.strip()=="PASS"
        assert not result.stderr.strip()
        assert calls==([] if case=="wrong_server_pid" else [True])
    finally:server.stop()
