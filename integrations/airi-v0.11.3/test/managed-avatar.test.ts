import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { loadManagedAvatarFromEnvironment } from '../src/managed-avatar.ts'

function vrmBytes(): Buffer {
  const document = Buffer.from(JSON.stringify({
    asset: { version: '2.0' },
    extensions: { VRM: {} },
  }))
  const padding = Buffer.alloc((4 - (document.length % 4)) % 4, 0x20)
  const json = Buffer.concat([document, padding])
  const header = Buffer.alloc(20)
  header.write('glTF', 0, 'ascii')
  header.writeUInt32LE(2, 4)
  header.writeUInt32LE(20 + json.length, 8)
  header.writeUInt32LE(json.length, 12)
  header.write('JSON', 16, 'ascii')
  return Buffer.concat([header, json])
}

test('loads a pinned managed VRM only for authenticated companion launches', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'managed-avatar-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const path = join(root, 'nemesia.vrm')
  const bytes = vrmBytes()
  await writeFile(path, bytes)

  assert.equal(await loadManagedAvatarFromEnvironment({}), undefined)
  assert.deepEqual(await loadManagedAvatarFromEnvironment({
    COMPANION_AVATAR_TOKEN: 'bridge-token',
    COMPANION_AVATAR_MODEL_PATH: path,
    COMPANION_AVATAR_MODEL_SHA256: createHash('sha256').update(bytes).digest('hex'),
    COMPANION_AVATAR_MODEL_ID: 'managed-nemesia',
    COMPANION_AVATAR_MODEL_NAME: 'Nemesia pajamas',
  }), {
    id: 'managed-nemesia',
    name: 'Nemesia pajamas',
    renderer: 'vrm',
    url: 'companion-avatar://managed/managed-nemesia.vrm',
    path,
  })
})

test('rejects an unpinned or non-VRM managed model', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'managed-avatar-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const path = join(root, 'nemesia.vrm')
  const bytes = vrmBytes()
  await writeFile(path, bytes)
  const base = {
    COMPANION_AVATAR_TOKEN: 'bridge-token',
    COMPANION_AVATAR_MODEL_PATH: path,
    COMPANION_AVATAR_MODEL_ID: 'managed-nemesia',
    COMPANION_AVATAR_MODEL_NAME: 'Nemesia pajamas',
  }

  await assert.rejects(loadManagedAvatarFromEnvironment({
    ...base,
    COMPANION_AVATAR_MODEL_SHA256: '0'.repeat(64),
  }), /digest/)
  await writeFile(path, Buffer.from('not a vrm'))
  await assert.rejects(loadManagedAvatarFromEnvironment({
    ...base,
    COMPANION_AVATAR_MODEL_SHA256: createHash('sha256').update('not a vrm').digest('hex'),
  }), /size|GLB/)
})
