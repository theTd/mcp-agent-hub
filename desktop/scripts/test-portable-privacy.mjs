import { test } from 'node:test';
import assert from 'node:assert/strict';
import { scanContent, checkPe } from './check-portable-privacy.mjs';

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
