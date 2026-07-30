import { createHash } from 'node:crypto'
import { open, realpath, stat } from 'node:fs/promises'
import { basename, extname, resolve } from 'node:path'

export interface ManagedAvatarEnvironment {
  COMPANION_AVATAR_TOKEN?: string
  COMPANION_AVATAR_MODEL_PATH?: string
  COMPANION_AVATAR_MODEL_SHA256?: string
  COMPANION_AVATAR_MODEL_ID?: string
  COMPANION_AVATAR_MODEL_NAME?: string
}

export interface ManagedAvatarModel {
  id: string
  name: string
  renderer: 'vrm'
  url: string
  path: string
}

const MODEL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const SHA256_PATTERN = /^[0-9a-f]{64}$/i
const MAX_VRM_BYTES = 512 * 1024 * 1024

export async function loadManagedAvatarFromEnvironment(
  environment: ManagedAvatarEnvironment,
): Promise<ManagedAvatarModel | undefined> {
  if (!environment.COMPANION_AVATAR_TOKEN?.trim())
    return undefined

  const configuredPath = environment.COMPANION_AVATAR_MODEL_PATH?.trim() ?? ''
  const expectedSha256 = environment.COMPANION_AVATAR_MODEL_SHA256?.trim() ?? ''
  const id = environment.COMPANION_AVATAR_MODEL_ID?.trim() ?? ''
  const name = environment.COMPANION_AVATAR_MODEL_NAME?.trim() ?? ''
  if (!configuredPath || !SHA256_PATTERN.test(expectedSha256) || !MODEL_ID_PATTERN.test(id))
    throw new Error('managed avatar configuration is incomplete')
  if (!name || name.length > 128)
    throw new Error('managed avatar name is invalid')

  const absolutePath = resolve(configuredPath)
  const canonicalPath = await realpath(absolutePath)
  if (canonicalPath.toLowerCase() !== absolutePath.toLowerCase())
    throw new Error('managed avatar path must not use a link')
  if (extname(canonicalPath).toLowerCase() !== '.vrm')
    throw new Error('managed avatar must use the .vrm extension')

  const metadata = await stat(canonicalPath)
  if (!metadata.isFile() || metadata.size < 20 || metadata.size > MAX_VRM_BYTES)
    throw new Error('managed avatar file size is invalid')

  const file = await open(canonicalPath, 'r')
  try {
    const header = Buffer.alloc(20)
    const { bytesRead } = await file.read(header, 0, header.length, 0)
    if (bytesRead !== header.length
      || header.toString('ascii', 0, 4) !== 'glTF'
      || header.readUInt32LE(4) !== 2
      || header.readUInt32LE(8) !== metadata.size
      || header.toString('ascii', 16, 20) !== 'JSON') {
      throw new Error('managed avatar is not a valid GLB 2.0 file')
    }
    const jsonLength = header.readUInt32LE(12)
    if (jsonLength < 2 || jsonLength > metadata.size - 20)
      throw new Error('managed avatar JSON chunk is invalid')

    const documentBuffer = Buffer.alloc(jsonLength)
    const documentRead = await file.read(documentBuffer, 0, jsonLength, 20)
    if (documentRead.bytesRead !== jsonLength)
      throw new Error('managed avatar JSON chunk is truncated')
    const document = JSON.parse(documentBuffer.toString('utf8').replace(/[\0\s]+$/u, '')) as {
      asset?: { version?: unknown }
      extensions?: Record<string, unknown>
    }
    if (document.asset?.version !== '2.0'
      || (!document.extensions?.VRM && !document.extensions?.VRMC_vrm)) {
      throw new Error('managed avatar does not contain VRM metadata')
    }

    const digest = createHash('sha256')
    const stream = file.createReadStream({ autoClose: false, start: 0 })
    for await (const chunk of stream)
      digest.update(chunk)
    if (digest.digest('hex') !== expectedSha256.toLowerCase())
      throw new Error('managed avatar digest does not match')
  }
  finally {
    await file.close()
  }

  return {
    id,
    name: name || basename(canonicalPath, extname(canonicalPath)),
    renderer: 'vrm',
    url: `companion-avatar://managed/${encodeURIComponent(id)}.vrm`,
    path: canonicalPath,
  }
}
