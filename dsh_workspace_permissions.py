"""Prepare a local DSH host's workspace without widening its confined token.

Only the host user's non-inheritable WRITE_OWNER ACE is added. Windows needs
this right to set the mandatory label DSH provisions alongside its write grant.
All native calls live here; callers receive value records, never Win32 handles.
"""
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
from urllib.parse import urlsplit
import uuid

WRITE_DAC = 0x40000
WRITE_OWNER = 0x80000
READ_CONTROL = 0x20000
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _aces(acl):
    if acl is None:
        raise ValueError("工作区使用空 DACL，拒绝自动修改")
    if len(acl) < 8 or struct.unpack_from("<H", acl, 2)[0] != len(acl):
        raise ValueError("无法识别工作区 DACL")
    count = struct.unpack_from("<H", acl, 4)[0]
    offset = 8
    result = []
    for _ in range(count):
        if offset + 4 > len(acl):
            raise ValueError("DACL ACE 越界")
        size = struct.unpack_from("<H", acl, offset + 2)[0]
        if size < 4 or offset + size > len(acl):
            raise ValueError("DACL ACE 长度无效")
        result.append(acl[offset:offset + size])
        offset += size
    return result


def _acl_with_entries(original, entries):
    size = 8 + sum(map(len, entries))
    if size > 65535:
        raise ValueError("DACL 空间不足")
    return struct.pack("<BBHHH", original[0], 0, size, len(entries), 0) + b"".join(entries)


def _owner_ace(sid):
    return struct.pack("<BBHI", 0, 0, 8 + len(sid), WRITE_OWNER) + sid


class WindowsWorkspaceAcl:
    """Small handle-based Win32 adapter. No subprocesses or shell interpolation."""

    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
        bindings = [
            (self.k, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (self.k, "CloseHandle", [w.HANDLE], w.BOOL),
            (self.k, "GetCurrentProcess", [], w.HANDLE),
            (self.k, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.k, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p),
            (self.k, "GetFinalPathNameByHandleW", [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD),
            (self.k, "GetFileInformationByHandleEx", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            (self.k, "GetDriveTypeW", [w.LPCWSTR], w.UINT),
            (self.k, "CreateMutexW", [ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
            (self.k, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            (self.k, "ReleaseMutex", [w.HANDLE], w.BOOL),
            (self.a, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
            (self.a, "GetTokenInformation", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.a, "GetLengthSid", [ctypes.c_void_p], w.DWORD),
            (self.a, "ConvertSidToStringSidW", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], w.BOOL),
            (self.a, "GetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], w.DWORD),
            (self.a, "SetSecurityInfo", [w.HANDLE, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], w.DWORD),
            (self.ip, "GetExtendedTcpTable", [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int, w.ULONG], w.DWORD),
        ]
        for dll, name, args, result in bindings:
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result

    @staticmethod
    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _sid(self, ptr):
        string = ctypes.c_void_p()
        self._check(self.a.ConvertSidToStringSidW(ptr, ctypes.byref(string)))
        try:
            return ctypes.wstring_at(string), ctypes.string_at(ptr, self.a.GetLengthSid(ptr))
        finally:
            self.k.LocalFree(string)

    def _token_user(self, process):
        token = w.HANDLE()
        self._check(self.a.OpenProcessToken(process, 8, ctypes.byref(token)))
        try:
            size = w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            self._check(self.a.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)))
            return self._sid(ctypes.c_void_p.from_buffer(buf).value)
        finally:
            self.k.CloseHandle(token)

    def host_identity(self, endpoint):
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if host != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    return None, "远程 DSH：未修改本机工作区权限"
            except ValueError:
                return None, "DSH 地址无法确认为本机：未修改权限"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return None, "无法核实 DSH 服务地址：未修改权限"
        pids = set()
        families = (2, 23) if host == "localhost" else ((23,) if ":" in host else (2,))
        for family in families:
            size = w.DWORD()
            code = self.ip.GetExtendedTcpTable(None, ctypes.byref(size), False, family, 3, 0)
            if code not in (0, 122):
                raise ctypes.WinError(code)
            for _ in range(3):
                table = ctypes.create_string_buffer(max(size.value, 4))
                code = self.ip.GetExtendedTcpTable(table, ctypes.byref(size), False, family, 3, 0)
                if code != 122:
                    break
            if code:
                raise ctypes.WinError(code)
            raw = table.raw
            row_size = 24 if family == 2 else 56
            count = struct.unpack_from("<I", raw)[0]
            for index in range(count):
                row = raw[4 + index * row_size:4 + (index + 1) * row_size]
                port_offset, pid_offset = (8, 20) if family == 2 else (20, 52)
                if len(row) != row_size or socket.ntohs(struct.unpack_from("<I", row, port_offset)[0] & 0xffff) != port:
                    continue
                address = ipaddress.ip_address(row[4:8] if family == 2 else row[:16])
                if address.is_unspecified or address.is_loopback and (host == "localhost" or address == ipaddress.ip_address(host)):
                    pids.add(struct.unpack_from("<I", row, pid_offset)[0])
        if not pids:
            return None, "找不到本机 DSH 监听进程，身份无法核实：未修改权限"
        current, sid_bytes = self._token_user(self.k.GetCurrentProcess())
        for pid in pids:
            process = self.k.OpenProcess(0x1000, False, pid)
            self._check(process)
            try:
                host_sid, _ = self._token_user(process)
            finally:
                self.k.CloseHandle(process)
            if host_sid != current:
                return None, "DSH 与 Hub 运行账户不同：未修改工作区权限"
        return (current, sid_bytes), "本机 DSH 与 Hub 运行账户一致"

    @contextmanager
    def lock(self, path):
        name = "Local\\mcp-agent-hub-dsh-acl-" + hashlib.sha256(path.casefold().encode()).hexdigest()
        mutex = self.k.CreateMutexW(None, False, name)
        self._check(mutex)
        acquired = False
        try:
            code = self.k.WaitForSingleObject(mutex, 5000)
            acquired = code in (0, 0x80)
            if not acquired:
                raise TimeoutError("工作区权限准备锁等待超时")
            yield
        finally:
            if acquired:
                self.k.ReleaseMutex(mutex)
            self.k.CloseHandle(mutex)

    def _open(self, path, access):
        # No FILE_SHARE_DELETE: pinned directories cannot be renamed under us.
        handle = self.k.CreateFileW(path, access, 3, None, 3, 0x02200000, None)
        if handle == INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    @contextmanager
    def workspace(self, path):
        handles = []
        try:
            parts = Path(path).parts
            if not parts or self.k.GetDriveTypeW(parts[0]) != 3:
                raise ValueError("仅支持本机固定磁盘上的工作区")
            for index in range(1, len(parts) + 1):
                current = str(Path(*parts[:index]))
                handle = self._open(current, READ_CONTROL | 0x80)
                handles.append(handle)
                info = (w.DWORD * 2)()
                self._check(self.k.GetFileInformationByHandleEx(handle, 9, info, ctypes.sizeof(info)))
                if info[0] & 0x400:
                    raise ValueError("工作区路径经过重解析点，拒绝自动修改权限")
                if not info[0] & 0x10:
                    raise ValueError("工作区必须是目录")
            buf = ctypes.create_unicode_buffer(32768)
            length = self.k.GetFinalPathNameByHandleW(handles[-1], buf, len(buf), 0)
            self._check(length and length < len(buf))
            final = buf.value.removeprefix("\\\\?\\")
            if os.path.normcase(final) != os.path.normcase(path):
                raise ValueError("目录句柄的最终路径与工作区不一致")
            yield handles[-1]
        finally:
            for handle in reversed(handles):
                self.k.CloseHandle(handle)

    def inspect(self, handle, path):
        owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        code = self.a.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        try:
            owner_sid, _ = self._sid(owner)
            acl = ctypes.string_at(dacl, ctypes.c_ushort.from_address(dacl.value + 2).value) if dacl.value else None
        finally:
            self.k.LocalFree(descriptor)
        rights = {}
        for name, mask in (("WRITE_DAC", WRITE_DAC), ("WRITE_OWNER", WRITE_OWNER)):
            try:
                check = self._open(path, mask)
            except OSError as error:
                if error.winerror != 5:
                    raise
                rights[name] = False
            else:
                self.k.CloseHandle(check)
                rights[name] = True
        # Conservatively refuse explicit denies of either preparation right, including
        # object/callback ACEs whose conditions we cannot safely evaluate here.
        denied = any(ace[0] in (1, 6, 10, 12) and not ace[1] & (0x10 | 0x08)
                     and len(ace) >= 8 and struct.unpack_from("<I", ace, 4)[0] & (WRITE_DAC | WRITE_OWNER | 0x10000000)
                     for ace in _aces(acl)) if acl is not None else False
        return {"owner_sid": owner_sid, "acl": acl, "rights": rights, "explicit_deny": denied}

    def write_acl(self, handle, path, acl):
        # Acquire WRITE_DAC against the still-pinned same directory. Set only
        # DACL_SECURITY_INFORMATION: owner, label and protection stay intact.
        writable = self._open(path, READ_CONTROL | WRITE_DAC)
        try:
            buf = ctypes.create_string_buffer(acl)
            code = self.a.SetSecurityInfo(writable, 1, 4, None, None, buf, None)
            if code:
                raise ctypes.WinError(code)
        finally:
            self.k.CloseHandle(writable)


def _safe_path(cwd):
    if not isinstance(cwd, str) or not cwd.strip() or not os.path.isabs(cwd):
        raise ValueError("工作区必须是明确的绝对路径")
    path = os.path.abspath(cwd)
    normalized = os.path.normcase(path)
    if normalized == os.path.normcase(os.path.dirname(path)) or normalized == os.path.normcase(os.path.expanduser("~")):
        raise ValueError("拒绝自动修改磁盘根目录或用户主目录")
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        root = os.environ.get(name)
        if root:
            root = os.path.normcase(os.path.abspath(root))
            if normalized == root or normalized.startswith(root + os.sep):
                raise ValueError("拒绝自动修改系统或应用程序目录")
    return path


def _save_record(path, record, *, create=False):
    # Outcome updates must never truncate the only durable recovery snapshot.
    target = path if create else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with target.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if not create:
            os.replace(target, path)
    finally:
        if not create:
            target.unlink(missing_ok=True)


def prepare_dsh_workspace(cwd, endpoint, data_dir, *, repair=True, backend=None):
    """Return ready/repaired/would_repair/blocked/skipped; preview never writes.

    A skipped unsupported host preserves DSH's existing launch behavior. A
    known local preparation failure blocks dispatch. Successful repair is a
    standing root-only grant; it does not claim native sandbox startup success.
    """
    result = {"status": "skipped", "allowed": True, "mode": "prepare" if repair else "check",
              "scope": "workspace_root_only", "missing_permissions": [], "reason": "", "diagnostic_id": None}
    if os.name != "nt" and backend is None:
        return dict(result, reason="非 Windows 环境：未修改工作区权限")
    try:
        backend = backend or WindowsWorkspaceAcl()
        identity, reason = backend.host_identity(endpoint)
    except Exception as error:
        return dict(result, reason=f"DSH 身份无法核实，未修改权限：{error}")
    if identity is None:
        return dict(result, reason=reason)
    sid, sid_bytes = identity
    result.update(status="blocked", allowed=False, host_sid=sid)
    try:
        path = _safe_path(cwd)
        result["workspace"] = path
        with backend.lock(path), backend.workspace(path) as handle:
            state = backend.inspect(handle, path)
            missing = [key for key, available in state["rights"].items() if not available]
            result["missing_permissions"] = missing
            if not missing:
                return dict(result, status="ready", allowed=True, reason="工作区所需权限已具备；沙箱启动由 DSH 原生事件确认")
            if state["owner_sid"] != sid:
                raise ValueError("工作区所有者与 DSH 宿主账户不同，未修改权限")
            if "WRITE_DAC" in missing or state["explicit_deny"]:
                raise ValueError("工作区缺少 WRITE_DAC 或存在显式拒绝，未修改权限")
            ace = _owner_ace(sid_bytes)
            entries = _aces(state["acl"])
            if ace in entries:
                raise ValueError("已有相同 WRITE_OWNER ACE 但权限仍不可用，未修改权限")
            if not repair:
                return dict(result, status="would_repair", allowed=True, reason="派发任务前将自动为宿主账户补齐根目录 WRITE_OWNER（不继承）")
            diagnostic_id = "dsh-acl-" + uuid.uuid4().hex
            record_path = Path(data_dir) / "diagnostics" / (diagnostic_id + ".json")
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"workspace": path, "host_sid": sid, "owner_sid": state["owner_sid"],
                      "before_dacl": base64.b64encode(state["acl"]).decode(),
                      "added_ace": base64.b64encode(ace).decode(), "status": "prepared"}
            _save_record(record_path, record, create=True)
            result["diagnostic_id"] = diagnostic_id
            attempted = False
            try:
                # Read again immediately before merge so other ACL edits survive.
                current = backend.inspect(handle, path)
                if current["owner_sid"] != sid or current["explicit_deny"] or not current["rights"]["WRITE_DAC"]:
                    raise ValueError("修改前工作区权限已变化，停止修复")
                if current["rights"]["WRITE_OWNER"]:
                    record["status"] = "already_ready"
                    _save_record(record_path, record)
                    return dict(result, status="ready", allowed=True, missing_permissions=[], reason="并发检查发现权限已具备")
                entries = _aces(current["acl"])
                if ace in entries:
                    raise ValueError("修改前出现相同 ACE，权限不可用；停止修复")
                record["before_dacl"] = base64.b64encode(current["acl"]).decode()
                record["status"] = "applying"
                _save_record(record_path, record)
                index = next((i for i, entry in enumerate(entries) if entry[0] not in (1, 6, 10, 12) or entry[1] & 0x10), len(entries))
                entries.insert(index, ace)
                attempted = True
                backend.write_acl(handle, path, _acl_with_entries(current["acl"], entries))
                verified = backend.inspect(handle, path)
                if verified["owner_sid"] != sid or not all(verified["rights"].values()) or ace not in _aces(verified["acl"]):
                    raise ValueError("新增 WRITE_OWNER 后有效权限验证失败")
                record["status"] = "repaired"
                _save_record(record_path, record)
                return dict(result, status="repaired", allowed=True, missing_permissions=[],
                            reason="已为宿主账户补齐根目录 WRITE_OWNER（不继承）；沙箱启动尚待原生确认")
            except Exception as error:
                record.update(status="failed", error=str(error))
                if attempted:
                    try:
                        latest = backend.inspect(handle, path)
                        entries = _aces(latest["acl"])
                        if ace in entries:
                            entries.remove(ace)
                            backend.write_acl(handle, path, _acl_with_entries(latest["acl"], entries))
                        if ace in _aces(backend.inspect(handle, path)["acl"]):
                            raise ValueError("新增 ACE 撤销后仍存在")
                        record["rollback"] = "confirmed"
                    except Exception as rollback_error:
                        record["rollback"] = "failed: " + str(rollback_error)
                try:
                    _save_record(record_path, record)
                except Exception as journal_error:
                    record["journal_error"] = str(journal_error)
                suffix = "；回滚：" + record["rollback"] if "rollback" in record else ""
                raise ValueError(str(error) + suffix) from error
    except Exception as error:
        return dict(result, status="blocked", allowed=False, reason=f"DSH 工作区权限准备失败：{error}")
