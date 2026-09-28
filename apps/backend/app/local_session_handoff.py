"""Current-user-only Windows local session handoff; never logs token material.

The caller supplies a short, local mint callback. Each connection can request one
bounded ASCII token. Network clients and preexisting pipe instances are rejected.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
import re
import threading
import time

_IO_TIMEOUT = 2.0
_POLL = 0.01
_REQUEST = b"MINT\n"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class HandoffError(RuntimeError):
    pass


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


class _SidAttributes(ctypes.Structure):
    _fields_ = [("Sid", w.LPVOID), ("Attributes", w.DWORD)]


class _WinApi:
    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        declarations = [
            (self.k,"GetCurrentProcess",[],w.HANDLE),
            (self.k,"CloseHandle",[w.HANDLE],w.BOOL),
            (self.k,"LocalFree",[w.LPVOID],w.LPVOID),
            (self.a,"OpenProcessToken",[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)],w.BOOL),
            (self.a,"GetTokenInformation",[w.HANDLE,w.DWORD,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD)],w.BOOL),
            (self.a,"ConvertSidToStringSidW",[w.LPVOID,ctypes.POINTER(w.LPWSTR)],w.BOOL),
            (self.a,"ConvertStringSecurityDescriptorToSecurityDescriptorW",[w.LPCWSTR,w.DWORD,ctypes.POINTER(w.LPVOID),ctypes.POINTER(w.DWORD)],w.BOOL),
            (self.k,"CreateNamedPipeW",[w.LPCWSTR,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,w.DWORD,ctypes.POINTER(_SecurityAttributes)],w.HANDLE),
            (self.k,"ConnectNamedPipe",[w.HANDLE,w.LPVOID],w.BOOL),
            (self.k,"DisconnectNamedPipe",[w.HANDLE],w.BOOL),
            (self.k,"ReadFile",[w.HANDLE,w.LPVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID],w.BOOL),
            (self.k,"WriteFile",[w.HANDLE,w.LPCVOID,w.DWORD,ctypes.POINTER(w.DWORD),w.LPVOID],w.BOOL),
        ]
        for dll,name,args,result in declarations:
            fn=getattr(dll,name);fn.argtypes=args;fn.restype=result

    def current_sid(self):
        token=w.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(),0x0008,ctypes.byref(token)):
            raise HandoffError("HANDOFF_SECURITY_UNAVAILABLE")
        try:
            size=w.DWORD()
            self.a.GetTokenInformation(token,1,None,0,ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise HandoffError("HANDOFF_SECURITY_UNAVAILABLE")
            buf=ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token,1,buf,size,ctypes.byref(size)):
                raise HandoffError("HANDOFF_SECURITY_UNAVAILABLE")
            sid=ctypes.cast(buf,ctypes.POINTER(_SidAttributes)).contents.Sid
            text=w.LPWSTR()
            if not self.a.ConvertSidToStringSidW(sid,ctypes.byref(text)):
                raise HandoffError("HANDOFF_SECURITY_UNAVAILABLE")
            try:return text.value
            finally:self.k.LocalFree(ctypes.cast(text,w.LPVOID))
        finally:self.k.CloseHandle(token)

    def create(self,path):
        descriptor=w.LPVOID()
        # Protected DACL: no inherited Everyone/Administrators entries, only TokenUser.
        sddl="D:P(A;;GA;;;"+self.current_sid()+")"
        if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,ctypes.byref(descriptor),None):
            raise HandoffError("HANDOFF_SECURITY_UNAVAILABLE")
        try:
            attrs=_SecurityAttributes(ctypes.sizeof(_SecurityAttributes),descriptor,False)
            # DUPLEX | FIRST_PIPE_INSTANCE; BYTE | NOWAIT | REJECT_REMOTE_CLIENTS.
            handle=self.k.CreateNamedPipeW(path,0x00000003|0x00080000,0x00000001|0x00000008,1,512,64,0,ctypes.byref(attrs))
            if handle == _INVALID_HANDLE:
                raise HandoffError("HANDOFF_PIPE_UNAVAILABLE")
            return handle
        finally:self.k.LocalFree(descriptor)


class _HandoffServer:
    def __init__(self,mint):
        self.pipe_name=f"AnxinBoard.LocalSession.{os.getpid()}"
        self.pipe_path="\\\\.\\pipe\\"+self.pipe_name
        self._api=_WinApi()
        self._handle=self._api.create(self.pipe_path)
        self._mint=mint
        self._stop=threading.Event()
        self._thread=threading.Thread(target=self._run,name="local-session-handoff",daemon=True)
        try:self._thread.start()
        except Exception:
            self._api.k.CloseHandle(self._handle)
            raise HandoffError("HANDOFF_START_FAILED") from None

    def stop(self):
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=_IO_TIMEOUT+1)

    def _connection(self):
        k=self._api.k;deadline=time.monotonic()+_IO_TIMEOUT;request=b""
        while not self._stop.is_set() and time.monotonic()<deadline:
            buf=ctypes.create_string_buffer(len(_REQUEST)+1);count=w.DWORD()
            ok=k.ReadFile(self._handle,buf,len(buf),ctypes.byref(count),None)
            if not ok and ctypes.get_last_error()!=232:
                return
            if count.value:
                request+=buf.raw[:count.value]
                if not _REQUEST.startswith(request):return
                if request==_REQUEST:break
            self._stop.wait(_POLL)
        if request!=_REQUEST or self._stop.is_set():return
        try:token=self._mint()
        except Exception:return
        if type(token) is not str or not _TOKEN.fullmatch(token) or self._stop.is_set():return
        raw=(token+"\n").encode("ascii")
        deadline=time.monotonic()+_IO_TIMEOUT;offset=0
        while not self._stop.is_set() and time.monotonic()<deadline and offset<len(raw):
            count=w.DWORD();part=raw[offset:]
            if not k.WriteFile(self._handle,part,len(part),ctypes.byref(count),None):return
            offset+=count.value
            if offset<len(raw):self._stop.wait(_POLL)
        # Do not FlushFileBuffers (it can block forever). Keep buffered response
        # available until the client closes or the connection deadline expires.
        while not self._stop.is_set() and time.monotonic()<deadline:
            buf=ctypes.create_string_buffer(1);count=w.DWORD()
            if not k.ReadFile(self._handle,buf,1,ctypes.byref(count),None) and ctypes.get_last_error()!=232:return
            if count.value:return
            self._stop.wait(_POLL)

    def _run(self):
        k=self._api.k
        try:
            while not self._stop.is_set():
                connected=k.ConnectNamedPipe(self._handle,None)
                error=ctypes.get_last_error() if not connected else 0
                if not connected and error==535:  # ERROR_PIPE_CONNECTED
                    self._connection()
                    k.DisconnectNamedPipe(self._handle)
                elif not connected and error in (109,232):
                    k.DisconnectNamedPipe(self._handle)
                elif not connected and error!=536:  # ERROR_PIPE_LISTENING
                    return
                self._stop.wait(_POLL)
        finally:k.CloseHandle(self._handle)


def start_handoff_server(mint):
    """Start one PID-bound pipe; raise fixed error if secure creation fails."""
    if os.name != "nt":return None
    if not callable(mint):raise HandoffError("HANDOFF_MINT_INVALID")
    return _HandoffServer(mint)
