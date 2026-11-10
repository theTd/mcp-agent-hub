"""Opt-in Windows acceptance using the installed DSH sandbox, with no model calls.

Use a NEW --root below a scratch directory. Only synthetic workspace ACLs are
changed; results and recovery records remain under that root for diagnosis.
"""
import argparse
import base64
import ctypes
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dsh_workspace_permissions import WindowsWorkspaceAcl, _aces, _acl_with_entries, prepare_dsh_workspace


PROBE = r'''
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
const [modulePath, workspace, outside] = process.argv.slice(2);
const { AclSandbox, workspaceWriteSid } = await import(pathToFileURL(modulePath).href);
const canonical = fs.realpathSync.native(workspace);
const sandbox = new AclSandbox({writableDirs: [canonical], tempDir: null,
  writeSid: workspaceWriteSid(canonical), mode: 'workspace-write'});
let result;
try {
  await sandbox.init();
  const script = `const fs = require('node:fs');
const inside = ${JSON.stringify(canonical + '\\inside.txt')};
if (fs.readFileSync(inside, 'utf8') !== 'seed') throw Error('read mismatch');
fs.writeFileSync(${JSON.stringify(canonical + '\\created.txt')}, 'sandbox-wrote');
try { fs.writeFileSync(inside, 'existing-wrote'); console.log('EXISTING_WRITE_OK'); }
catch (error) { if (!['EACCES', 'EPERM'].includes(error.code)) throw error; console.log('EXISTING_WRITE_DENIED'); }
try { fs.writeFileSync(${JSON.stringify(outside)}, 'must-not-write'); throw Error('OUTSIDE_WRITE_ALLOWED'); }
catch (error) { if (!['EACCES', 'EPERM'].includes(error.code)) throw error; console.log('OUTSIDE_DENIED'); }
console.log('INSIDE_READ_CREATE_OK');`;
  const child = sandbox.spawn({command: process.execPath, args: ['-e', script], cwd: canonical});
  const settled = await child.wait();
  result = {initialized: true, exitCode: settled.exitCode,
    stdout: settled.stdout.toString('utf8'), stderr: settled.stderr.toString('utf8')};
} catch (error) { result = {initialized: false, error: String(error)}; }
finally { try { sandbox.dispose(); } catch (error) { (result ??= {}).disposeError = String(error); } }
console.log(JSON.stringify(result));
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--sandbox-module", required=True, type=Path)
    parser.add_argument("--dsh-url", default="http://127.0.0.1:3080")
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    root = args.root.absolute()
    root.mkdir(exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("seed", encoding="utf-8")
    child_dir = workspace / "child"
    child_dir.mkdir()
    outside = root / "outside.txt"
    probe = root / "probe.mjs"
    probe.write_text(PROBE, encoding="utf-8")
    native = WindowsWorkspaceAcl()
    identity, reason = native.host_identity(args.dsh_url)
    if not identity:
        raise RuntimeError(reason)
    sid, sid_bytes = identity
    report = {"workspace": str(workspace), "host_sid": sid}
    with native.workspace(str(workspace)) as handle:
        original = native.inspect(handle, str(workspace))["acl"]
    try:
        # A user-owned Modify directory: implicit WRITE_DAC, no WRITE_OWNER.
        # Inheritance protection is fixture setup only, never production repair.
        modify = struct.pack("<BBHI", 0, 3, 8 + len(sid_bytes), 0x1301bf) + sid_bytes
        fixture_acl = _acl_with_entries(original, [modify])
        writable = native._open(str(workspace), 0x60000)
        try:
            code = native.a.SetSecurityInfo(writable, 1, 0x80000004, None, None, ctypes.create_string_buffer(fixture_acl), None)
            if code:
                raise ctypes.WinError(code)
        finally:
            native.k.CloseHandle(writable)
        report["explicit_deny_blocks"] = {}
        for name, mask in (("WRITE_DAC", 0x40000), ("WRITE_OWNER", 0x80000)):
            deny = struct.pack("<BBHI", 1, 0, 8 + len(sid_bytes), mask) + sid_bytes
            denied_acl = _acl_with_entries(fixture_acl, [deny, modify])
            with native.workspace(str(workspace)) as handle:
                native.write_acl(handle, str(workspace), denied_acl)
                denied = prepare_dsh_workspace(str(workspace), args.dsh_url, root)
                assert denied["status"] == "blocked", denied
                assert native.inspect(handle, str(workspace))["acl"] == denied_acl
                native.write_acl(handle, str(workspace), fixture_acl)
            report["explicit_deny_blocks"][name] = True
        with native.workspace(str(child_dir)) as child_handle:
            child_before = native.inspect(child_handle, str(child_dir))["acl"]
        with native.workspace(str(workspace)) as handle:
            before = native.inspect(handle, str(workspace))
        assert before["rights"] == {"WRITE_DAC": True, "WRITE_OWNER": False}, before["rights"]
        report["before_rights"] = before["rights"]

        def run_native():
            process = subprocess.run([args.node, str(probe), str(args.sandbox_module.absolute()),
                                      str(workspace), str(outside)],
                                     capture_output=True, text=True, encoding="utf-8", timeout=45)
            if process.returncode:
                raise RuntimeError(process.stderr)
            return json.loads(process.stdout.strip().splitlines()[-1])

        report["native_before"] = run_native()
        assert not report["native_before"]["initialized"], report["native_before"]
        assert "grantWrite" in report["native_before"]["error"] and "Win32 5" in report["native_before"]["error"]
        preview = prepare_dsh_workspace(str(workspace), args.dsh_url, root, repair=False)
        assert preview["status"] == "would_repair", preview
        report["preview"] = preview
        assert not (root / "diagnostics").exists()
        prepared = prepare_dsh_workspace(str(workspace), args.dsh_url, root)
        assert prepared["status"] == "repaired", prepared
        report["preparation"] = prepared
        with native.workspace(str(workspace)) as handle:
            after = native.inspect(handle, str(workspace))
        assert after["owner_sid"] == before["owner_sid"]
        added = [ace for ace in _aces(after["acl"]) if ace not in _aces(before["acl"])]
        assert len(added) == 1 and added[0][1] == 0 and struct.unpack_from("<I", added[0], 4)[0] == 0x80000
        with native.workspace(str(child_dir)) as child_handle:
            assert native.inspect(child_handle, str(child_dir))["acl"] == child_before, "repair changed child DACL"
        report["child_dacl_unchanged"] = True
        report["native_after"] = run_native()
        with native.workspace(str(workspace)) as handle:
            report["native_after_workspace_dacl"] = base64.b64encode(native.inspect(handle, str(workspace))["acl"]).decode()
        file_handle = native._open(str(inside), 0x20000)
        try:
            report["native_after_file_dacl"] = base64.b64encode(native.inspect(file_handle, str(inside))["acl"]).decode()
        finally:
            native.k.CloseHandle(file_handle)
        native_after = report["native_after"]
        assert native_after["initialized"] and native_after["exitCode"] == 0, native_after
        assert "OUTSIDE_DENIED" in native_after["stdout"] and "INSIDE_READ_CREATE_OK" in native_after["stdout"]
        assert (workspace / "created.txt").read_text(encoding="utf-8") == "sandbox-wrote"
        report["existing_file_write"] = "allowed" if "EXISTING_WRITE_OK" in native_after["stdout"] else "denied"
        assert not outside.exists()
        report["idempotent"] = prepare_dsh_workspace(str(workspace), args.dsh_url, root)["status"] == "ready"
        assert report["idempotent"]
        report["passed"] = True
    finally:
        writable = native._open(str(workspace), 0x60000)
        try:
            code = native.a.SetSecurityInfo(writable, 1, 0x20000004, None, None, ctypes.create_string_buffer(original), None)
            if code:
                raise ctypes.WinError(code)
        finally:
            native.k.CloseHandle(writable)
        (root / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Only this freshly created, verified fixture is removed; diagnostics stay.
        assert workspace.parent == root and workspace.name == "workspace"
        shutil.rmtree(workspace)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
