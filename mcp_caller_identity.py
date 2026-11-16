"""Which process, and which hub session, made an MCP request to the hub.

The MCP session id is no owner key: on 2026-07-28 protocol connections every request gets a new
one. The caller's process is stable for its whole life, so the hub finds it from the request's TCP
peer (the loopback connection's owning PID, from GetExtendedTcpTable) and walks its parent chain
to a hub session's process. An external parent (a Claude Code window) is known by its PID and
executable; a parent the hub itself started is known by its session id.

Windows only; elsewhere, and on any failure, the caller is unknown (None).
"""
from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
import sys
from ctypes import wintypes as w
from typing import Dict, Mapping, Optional, Tuple

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_CONNECTIONS = 4
ERROR_INSUFFICIENT_BUFFER = 122
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = w.HANDLE(-1).value
# Deep enough for engine -> node/bun helper -> shell wrappers; a cycle from PID reuse stops it.
MAX_ANCESTOR_DEPTH = 16


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


def _tcp_rows(family: int) -> bytes:
    ip = ctypes.WinDLL("iphlpapi")
    get_table = ip.GetExtendedTcpTable
    get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG]
    get_table.restype = w.DWORD
    size = w.DWORD(0)
    code = get_table(None, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
    for _ in range(4):
        if code not in (0, ERROR_INSUFFICIENT_BUFFER):
            break
        table = ctypes.create_string_buffer(max(size.value, 4))
        code = get_table(table, ctypes.byref(size), False, family, TCP_TABLE_OWNER_PID_CONNECTIONS, 0)
        if code == 0:
            return table.raw
    raise OSError(f"GetExtendedTcpTable failed with {code}")


def _port(raw: bytes, offset: int) -> int:
    return socket.ntohs(struct.unpack_from("<I", raw, offset)[0] & 0xFFFF)


def find_connection_owner(raw: bytes, family: int, local_port: int, remote_port: int) -> Optional[int]:
    """The owning PID of the table row whose local and remote ports match (MIB_TCP[6]ROW_OWNER_PID)."""
    row_size, local_off, remote_off, pid_off = (24, 8, 16, 20) if family == AF_INET else (56, 20, 44, 52)
    count = struct.unpack_from("<I", raw)[0] if len(raw) >= 4 else 0
    for index in range(count):
        start = 4 + index * row_size
        if start + row_size > len(raw):
            break
        if _port(raw, start + local_off) == local_port and _port(raw, start + remote_off) == remote_port:
            return struct.unpack_from("<I", raw, start + pid_off)[0]
    return None


def peer_pid(peer_host: str, peer_port: int, server_port: int) -> Optional[int]:
    """The PID owning the client end of a loopback connection to the hub (peer_port -> server_port)."""
    try:
        address = ipaddress.ip_address(peer_host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    family = AF_INET6 if address.version == 6 else AF_INET
    return find_connection_owner(_tcp_rows(family), family, int(peer_port), int(server_port))


def process_table() -> Dict[int, Tuple[int, str]]:
    """pid -> (parent pid, executable name) for every process, from one Toolhelp snapshot."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    table: Dict[int, Tuple[int, str]] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return table


def owning_session(pid: int, processes: Mapping[int, Tuple[int, str]],
                   session_pids: Mapping[int, str]) -> Optional[str]:
    """The hub session whose process is `pid` or one of its ancestors."""
    seen = set()
    current = pid
    for _ in range(MAX_ANCESTOR_DEPTH):
        if current in session_pids:
            return session_pids[current]
        if current in seen or current not in processes:
            return None
        seen.add(current)
        current = processes[current][0]
    return None


def resolve_caller(peer_host: str, peer_port: int, server_port: int,
                   session_pids: Mapping[int, str]) -> Optional[Dict[str, object]]:
    """{"pid", "process", "hub_session_id"} of the caller, or None when it cannot be told."""
    if sys.platform != "win32":
        return None
    try:
        pid = peer_pid(peer_host, peer_port, server_port)
        if not pid:
            return None
        processes = process_table()
    except (OSError, ValueError, AttributeError):
        return None
    return {
        "pid": pid,
        "process": processes.get(pid, (0, ""))[1],
        "hub_session_id": owning_session(pid, processes, session_pids),
    }


def same_caller(a: Optional[Mapping[str, object]], b: Optional[Mapping[str, object]]) -> bool:
    """Two callers are the same parent: the same hub session, else the same external process."""
    if not a or not b:
        return False
    if a.get("hub_session_id") or b.get("hub_session_id"):
        return a.get("hub_session_id") == b.get("hub_session_id")
    return bool(a.get("pid")) and a.get("pid") == b.get("pid") and a.get("process") == b.get("process")
