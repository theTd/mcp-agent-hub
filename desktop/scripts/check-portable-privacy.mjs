// Release gate: scan uncompressed resources as well as the actual PE, without printing private values.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function decodedTexts(bytes, markers) {
  // Bound regex input size, including arbitrary binary interpreted as UTF-16.
  const decoded = [bytes.toString('utf8'), bytes.toString('latin1'), bytes.subarray(0, Math.floor(bytes.length / 2) * 2).toString('utf16le'), bytes.subarray(1, 1 + Math.floor((bytes.length - 1) / 2) * 2).toString('utf16le')];
  const overlap = Math.max(1024, ...markers.map(([, value]) => (value?.length || 0) + 2));
  return decoded.flatMap(text => { const chunks = []; for (let i = 0; i < text.length; i += 4096) chunks.push(text.slice(Math.max(0, i - overlap), i + 4096)); return chunks; });
}

function scanMarkers(texts, markers) {
  for (const [label, value, word] of markers) {
    if (!value || value.length < 3) continue;
    const pattern = escape(value).replaceAll('\\\\', '[\\\\/]');
    const matcher = new RegExp(word ? `(?<![A-Za-z0-9_])${pattern}(?![A-Za-z0-9_])` : pattern, 'i');
    if (texts.some(text => matcher.test(text))) throw new Error(`Privacy gate rejected ${label}`);
  }
}

function scanCredentials(texts) {
  for (const text of texts) {
    if (/[A-Z]:[\\/]Users[\\/](?!me[\\/]|Public[\\/]|Default[\\/])[^\s"'<>\\/]+[\\/]/i.test(text)) throw new Error('Privacy gate rejected an absolute user directory');
    if (/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b/.test(text)) throw new Error('Privacy gate rejected credential material');
  }
}

export function scanContent(bytes, markers) {
  const texts = decodedTexts(bytes, markers);
  scanMarkers(texts, markers);
  scanCredentials(texts);
}

export function checkPe(bytes) {
  const pe = bytes.readUInt32LE(0x3c);
  if (bytes.toString('ascii', pe, pe + 4) !== 'PE\0\0' || bytes.readUInt16LE(pe + 4) !== 0x8664) throw new Error('Expected a Windows x64 PE');
  const optional = pe + 24;
  if (bytes.readUInt16LE(optional) !== 0x20b) throw new Error('Expected PE32+');
  const debugRva = bytes.readUInt32LE(optional + 112 + 6 * 8);
  const debugSize = bytes.readUInt32LE(optional + 116 + 6 * 8);
  const sections = optional + bytes.readUInt16LE(pe + 20);
  const toOffset = rva => {
    for (let i = 0; i < bytes.readUInt16LE(pe + 6); i++) {
      const s = sections + i * 40;
      const va = bytes.readUInt32LE(s + 12);
      if (rva >= va && rva < va + Math.max(bytes.readUInt32LE(s + 8), bytes.readUInt32LE(s + 16))) return bytes.readUInt32LE(s + 20) + rva - va;
    }
    throw new Error('Invalid PE debug directory');
  };
  if (debugRva && debugSize) {
    const debug = toOffset(debugRva);
    for (let i = 0; i + 28 <= debugSize; i += 28) {
      if (bytes.readUInt32LE(debug + i + 12) !== 2) continue;
      const raw = bytes.readUInt32LE(debug + i + 24);
      if (bytes.toString('ascii', raw, raw + 4) !== 'RSDS') throw new Error('Unsupported CodeView record');
      const pdb = bytes.subarray(raw + 24, raw + bytes.readUInt32LE(debug + i + 16)).toString('utf8').split('\0')[0];
      if (/^[A-Za-z]:|^[\\/]/.test(pdb)) throw new Error('Privacy gate rejected an absolute PDB path');
    }
  }
}

const IMAGE_SCN_MEM_EXECUTE = 0x20000000;
const BINARY_ONLY_SECTIONS = new Set(['.pdata', '.reloc']);

function peSections(bytes) {
  const pe = bytes.readUInt32LE(0x3c);
  const count = bytes.readUInt16LE(pe + 6);
  const table = pe + 24 + bytes.readUInt16LE(pe + 20);
  const sections = [];
  for (let i = 0; i < count; i++) {
    const header = table + i * 40;
    sections.push({
      name: bytes.toString('latin1', header, header + 8).split('\0', 1)[0],
      size: bytes.readUInt32LE(header + 16),
      offset: bytes.readUInt32LE(header + 20),
      characteristics: bytes.readUInt32LE(header + 36),
    });
  }
  return sections;
}

function mergeRanges(ranges) {
  const merged = [];
  for (const [start, end] of ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
    const last = merged[merged.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

// Executable code and unwind/relocation tables hold opcodes and RVAs, not strings,
// so a short word such as a 3-character username can occur there by chance.
function binaryOnlyRanges(bytes) {
  const ranges = [];
  for (const section of peSections(bytes)) {
    if (!(section.characteristics & IMAGE_SCN_MEM_EXECUTE) && !BINARY_ONLY_SECTIONS.has(section.name)) continue;
    const start = Math.min(Math.max(section.offset, 0), bytes.length);
    const end = Math.min(Math.max(section.offset + section.size, start), bytes.length);
    if (end > start) ranges.push([start, end]);
  }
  return mergeRanges(ranges);
}

function complementRanges(ranges, length) {
  const result = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) result.push([cursor, start]);
    cursor = Math.max(cursor, end);
  }
  if (cursor < length) result.push([cursor, length]);
  return result;
}

export function scanPeContent(bytes, markers) {
  checkPe(bytes);
  const everywhere = [];
  const shortWords = [];
  for (const marker of markers) {
    const [, value, word] = marker;
    if (word && (value?.length || 0) < 5) shortWords.push(marker);
    else everywhere.push(marker);
  }
  const allTexts = decodedTexts(bytes, everywhere);
  scanMarkers(allTexts, everywhere);
  scanCredentials(allTexts);
  if (!shortWords.length) return;
  // Short words are only meaningful in headers, data/resources and overlay.
  for (const [start, end] of complementRanges(binaryOnlyRanges(bytes), bytes.length)) {
    scanMarkers(decodedTexts(bytes.subarray(start, end), shortWords), shortWords);
  }
}

function hostMarkers() {
  let common;
  try { common = execFileSync('git', ['-c', `safe.directory=${root.replaceAll('\\', '/')}`, 'rev-parse', '--path-format=absolute', '--git-common-dir'], { cwd: root, encoding: 'utf8' }).trim(); }
  catch { throw new Error('Cannot determine source repository for privacy checking'); }
  return [
    ['build workspace', root], ['original repository', path.dirname(common)],
    ['home directory', os.homedir()], ['Cargo cache', process.env.CARGO_HOME],
    ['workspace owner', root.match(/[\\/]Users[\\/]([^\\/]+)/i)?.[1], true],
    ['build username', os.userInfo().username, true], ['build machine name', os.hostname(), true],
  ];
}

export function checkRelease(exe, report) {
  const markers = hostMarkers();
  const names = fs.readFileSync(path.join(root, 'desktop/portable/payload.txt'), 'utf8').trim().split(/\r?\n/);
  const sources = [...names, 'desktop/splash/index.html', 'desktop/splash/splash.css', 'desktop/splash/splash.js', 'desktop/splash/hub-icon.svg'];
  const hashes = {};
  for (const name of sources) {
    if (process.env.PRIVACY_DEBUG) console.error(`Scanning ${name}`);
    if (name.includes('..') || name.includes(':') || name.includes('\\') || /(?:^|\/)(?:config\.json|agent_profiles\.json)|\.(?:db|sqlite|log|exe|pdb)$/i.test(name)) throw new Error('Forbidden payload member');
    const full = path.join(root, name);
    if (fs.lstatSync(full).isSymbolicLink()) throw new Error('Symbolic payload member');
    const bytes = fs.readFileSync(full);
    try {
      if (name.toLowerCase().endsWith('.pyd')) scanPeContent(bytes, markers);
      else scanContent(bytes, markers);
    } catch (error) { throw new Error(`${name}: ${error.message}`); }
    hashes[name] = crypto.createHash('sha256').update(bytes).digest('hex');
  }
  let exeHash;
  if (exe) {
    const bytes = fs.readFileSync(exe);
    scanPeContent(bytes, markers);
    exeHash = crypto.createHash('sha256').update(bytes).digest('hex');
  }
  const result = { passed: true, exe_sha256: exeHash, source_files: hashes, checks: ['UTF-8/ASCII/UTF-16', 'host identifiers and paths', 'credentials', 'PE section-aware short identifier scan', 'explicit resource manifest', 'x64 PE and CodeView/PDB for .pyd payloads', ...(exe ? ['x64 PE and CodeView/PDB'] : [])] };
  if (report) fs.writeFileSync(report, JSON.stringify(result, null, 2) + '\n');
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const arg = name => { const i = process.argv.indexOf(name); return i < 0 ? undefined : process.argv[i + 1]; };
    const result = checkRelease(arg('--exe'), arg('--report'));
    console.log(`Privacy gate passed: ${Object.keys(result.source_files).length} resources${result.exe_sha256 ? ' and x64 EXE' : ''}`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
