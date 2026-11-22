import { test } from 'node:test';
import assert from 'node:assert/strict';
import { scanContent, scanPeContent, checkPe } from './check-portable-privacy.mjs';

test('rejects private paths in UTF-8 and either UTF-16 alignment', () => {
  for (const text of ['C:\\Users\\release-test\\repo', 'C:/Users/release-test/repo']) {
    for (const encoding of ['utf8', 'utf16le']) {
      for (const prefix of [Buffer.alloc(0), Buffer.from([0xff])]) {
        assert.throws(() => scanContent(Buffer.concat([prefix, Buffer.from(text, encoding)]), [['test home', 'C:\\Users\\release-test']]));
      }
    }
  }
});
test('rejects usernames, machine names and private keys while accepting generic examples', () => {
  assert.throws(() => scanContent(Buffer.from('built by release-user'), [['username', 'release-user', true]]));
  assert.throws(() => scanContent(Buffer.from('MACHINE-SECRET'), [['machine', 'MACHINE-SECRET', true]]));
  assert.throws(() => scanContent(Buffer.from('-----BEGIN PRIVATE KEY-----'), []));
  assert.doesNotThrow(() => scanContent(Buffer.from('C:\\Users\\me\\bin\\python.exe'), []));
});

test('detects identifiers across scan chunk boundaries', () => {
  assert.throws(() => scanContent(Buffer.from('x'.repeat(4090) + 'C:\\Users\\release-test\\repo'), [['home', 'C:\\Users\\release-test']]));
});

test('rejects absolute CodeView PDB paths and accepts a basename', () => {
  function fixture(pdb) {
    const bytes = Buffer.alloc(2048);
    bytes.writeUInt32LE(128, 0x3c);
    bytes.write('PE\0\0', 128, 'ascii');
    bytes.writeUInt16LE(0x8664, 132);
    bytes.writeUInt16LE(1, 134);
    bytes.writeUInt16LE(240, 148);
    bytes.writeUInt16LE(0x20b, 152);
    bytes.writeUInt32LE(0x1000, 312);
    bytes.writeUInt32LE(28, 316);
    bytes.writeUInt32LE(0x1000, 400);
    bytes.writeUInt32LE(0x1000, 404);
    bytes.writeUInt32LE(0x400, 412);
    bytes.writeUInt32LE(2, 0x400 + 12);
    bytes.writeUInt32LE(25 + pdb.length, 0x400 + 16);
    bytes.writeUInt32LE(0x500, 0x400 + 24);
    bytes.write('RSDS', 0x500, 'ascii');
    bytes.write(pdb, 0x500 + 24, 'utf8');
    return bytes;
  }
  assert.throws(() => checkPe(fixture('E:\\private-build\\hub.pdb')), /absolute PDB/);
  assert.doesNotThrow(() => checkPe(fixture('hub.pdb')));
});

function peFixture(sections, size = 0x800) {
  const bytes = Buffer.alloc(size);
  bytes.writeUInt32LE(0x80, 0x3c);
  bytes.write('PE\0\0', 0x80, 'ascii');
  bytes.writeUInt16LE(0x8664, 0x84);
  bytes.writeUInt16LE(sections.length, 0x86);
  bytes.writeUInt16LE(240, 0x94);
  bytes.writeUInt16LE(0x20b, 0x98);
  const table = 0x98 + 240;
  sections.forEach((section, i) => {
    const header = table + i * 40;
    bytes.write(section.name, header, 'latin1');
    bytes.writeUInt32LE(section.size ?? 0x100, header + 8);
    bytes.writeUInt32LE(0x1000 + i * 0x1000, header + 12);
    bytes.writeUInt32LE(section.size ?? 0x100, header + 16);
    bytes.writeUInt32LE(section.offset ?? 0, header + 20);
    bytes.writeUInt32LE(section.characteristics ?? 0x40000040, header + 36);
  });
  return bytes;
}

const textSection = { name: '.text', offset: 0x200, size: 0x100, characteristics: 0x60000020 };
const rdataSection = { name: '.rdata', offset: 0x300, size: 0x100, characteristics: 0x40000040 };
const cui = [['build username', 'cui', true]];

test('ignores short word markers inside executable code', () => {
  const bytes = peFixture([textSection, rdataSection]);
  Buffer.from([0x4c, 0x8d, 0x0d, 0x63, 0x75, 0x69, 0x00]).copy(bytes, 0x200);
  assert.doesNotThrow(() => checkPe(bytes));
  assert.doesNotThrow(() => scanPeContent(bytes, cui));
});

test('ignores short word markers inside .pdata and .reloc tables', () => {
  const bytes = peFixture([
    textSection,
    { name: '.pdata', offset: 0x300, size: 0x100, characteristics: 0x40000040 },
    { name: '.reloc', offset: 0x400, size: 0x100, characteristics: 0x42000040 },
  ]);
  bytes.write('cui', 0x300, 'latin1');
  bytes.write('cui', 0x402, 'latin1');
  assert.doesNotThrow(() => scanPeContent(bytes, cui));
});

test('rejects short word markers in data sections, headers and overlay', () => {
  for (const offset of [0x1e0, 0x300, 0x7f0]) {
    const bytes = peFixture([textSection, rdataSection]);
    bytes.write('cui', offset, 'latin1');
    assert.throws(() => scanPeContent(bytes, cui), /build username/, `offset 0x${offset.toString(16)}`);
  }
});

test('rejects long identifiers, paths and credentials even inside executable code', () => {
  const long = peFixture([textSection, rdataSection]);
  long.write('built-by-builduser', 0x200, 'latin1');
  assert.throws(() => scanPeContent(long, [['build username', 'builduser', true]]), /build username/);

  const home = peFixture([textSection, rdataSection]);
  home.write('C:\\Users\\cui\\repo', 0x200, 'latin1');
  assert.throws(() => scanPeContent(home, cui), /absolute user directory/);

  const key = peFixture([textSection, rdataSection]);
  key.write('-----BEGIN PRIVATE KEY-----', 0x200, 'latin1');
  assert.throws(() => scanPeContent(key, cui), /credential material/);
});

test('scans data despite zero-size or out-of-range executable sections', () => {
  const zero = peFixture([{ ...textSection, size: 0 }]);
  zero.write('cui', 0x200, 'latin1');
  assert.throws(() => scanPeContent(zero, cui), /build username/);

  const beyond = peFixture([{ ...textSection, offset: 0x9000 }]);
  beyond.write('cui', 0x200, 'latin1');
  assert.throws(() => scanPeContent(beyond, cui), /build username/);
});
