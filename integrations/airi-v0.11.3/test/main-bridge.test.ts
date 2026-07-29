import assert from 'node:assert/strict'
import { createServer } from 'node:net'
import test from 'node:test'

import type { AvatarStageAdapter } from '../src/protocol.ts'

import { closePeer, startAvatarBridgeServer, startAvatarBridgeServerFromEnvironment } from '../src/main-bridge.ts'

function adapter(overrides: Partial<AvatarStageAdapter> = {}): AvatarStageAdapter {
  return {
    health: async () => ({ status: 'healthy' }),
    listModels: async () => ({ models: [] }),
    validateModel: async () => ({ errors: [] }),
    loadModel: async () => ({ loaded: true }),
    updateState: async () => ({}),
    triggerExpression: async () => ({}),
    triggerGesture: async () => ({}),
    setProactiveLevel: async () => ({}),
    inspectStage: async () => ({ renderer: 'live2d' }),
    ...overrides,
  }
}

function rpc(id: string, method: string, params: Record<string, unknown> = {}) {
  return JSON.stringify({
    protocol: 'companion-avatar',
    version: 1,
    type: 'request',
    id,
    method,
    params,
  })
}

async function nextMessage(socket: WebSocket): Promise<Record<string, unknown>> {
  return await new Promise((resolve, reject) => {
    socket.addEventListener('message', event => resolve(JSON.parse(String(event.data))), { once: true })
    socket.addEventListener('error', () => reject(new Error('websocket failed')), { once: true })
  })
}

async function sendAndReceive(socket: WebSocket, payload: string): Promise<Record<string, unknown>> {
  const response = nextMessage(socket)
  socket.send(payload)
  return await response
}

async function closeSocket(socket: WebSocket): Promise<void> {
  if (socket.readyState === WebSocket.CLOSED)
    return
  const closed = new Promise<void>(resolve => socket.addEventListener('close', () => resolve(), { once: true }))
  socket.close()
  await closed
}

async function openSocket(url: string): Promise<WebSocket> {
  const socket = new WebSocket(url)
  await new Promise<void>((resolve, reject) => {
    socket.addEventListener('open', () => resolve(), { once: true })
    socket.addEventListener('error', () => reject(new Error('websocket failed')), { once: true })
  })
  return socket
}

async function availablePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  if (!address || typeof address === 'string')
    throw new Error('failed to allocate test port')
  return address.port
}

test('serves authenticated protocol requests over a loopback websocket', async () => {
  const server = await startAvatarBridgeServer({
    expectedToken: 'bridge-token', adapter: adapter(), port: await availablePort(),
  })
  const socket = await openSocket(server.url)
  try {
    assert.equal((await sendAndReceive(socket, rpc('handshake', 'handshake', {
      supported_versions: [1], client: 'virtual-companion', auth_token: 'bridge-token',
    }))).ok, true)
    assert.deepEqual((await sendAndReceive(socket, rpc('health', 'health'))).result, { status: 'healthy' })
  }
  finally {
    await closeSocket(socket)
    await server.close()
  }
})

test('closes unauthenticated clients after a rejected request', async () => {
  const server = await startAvatarBridgeServer({
    expectedToken: 'bridge-token', adapter: adapter(), port: await availablePort(),
  })
  const socket = await openSocket(server.url)
  try {
    const response = nextMessage(socket)
    const closed = new Promise<CloseEvent>(resolve => socket.addEventListener('close', resolve, { once: true }))
    socket.send(rpc('pre-auth', 'health'))
    assert.equal((await response).ok, false)
    assert.equal((await closed).code, 1008)
  }
  finally {
    await closeSocket(socket)
    await server.close()
  }
})

test('closes malformed and oversized connections without reflecting input', async (t) => {
  const server = await startAvatarBridgeServer({
    expectedToken: 'bridge-token', adapter: adapter(), port: await availablePort(), maxMessageBytes: 1024,
  })
  t.after(() => server.close())

  for (const payload of ['{', 'x'.repeat(1025), new Uint8Array(1025)]) {
    const socket = await openSocket(server.url)
    const closed = new Promise<CloseEvent>(resolve => socket.addEventListener('close', resolve, { once: true }))
    socket.send(payload)
    const event = await closed
    assert.ok(event.code === 1008 || event.code === 1009)
    const sample = typeof payload === 'string' ? payload.slice(0, 10) : 'binary input'
    assert.equal(event.reason.includes(sample), false)
  }
})

test('enforces a per-connection in-flight request limit', async () => {
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  const server = await startAvatarBridgeServer({
    expectedToken: 'bridge-token',
    adapter: adapter({ health: async () => { await blocked; return { status: 'healthy' } } }),
    port: await availablePort(),
    maxConcurrentRequestsPerConnection: 1,
  })
  const socket = await openSocket(server.url)
  try {
    await sendAndReceive(socket, rpc('handshake', 'handshake', {
      supported_versions: [1], client: 'virtual-companion', auth_token: 'bridge-token',
    }))

    const closed = new Promise<CloseEvent>(resolve => socket.addEventListener('close', resolve, { once: true }))
    socket.send(rpc('health-1', 'health'))
    socket.send(rpc('health-2', 'health'))
    assert.equal((await closed).code, 1008)
  }
  finally {
    release()
    await closeSocket(socket)
    await server.close()
  }
})

test('server shutdown closes active peers and is idempotent', async () => {
  const server = await startAvatarBridgeServer({
    expectedToken: 'bridge-token', adapter: adapter(), port: await availablePort(),
  })
  const socket = await openSocket(server.url)
  const closed = new Promise<CloseEvent>(resolve => socket.addEventListener('close', resolve, { once: true }))
  await server.close()
  assert.equal((await closed).code, 1001)
  await server.close()
})

test('peer close failures remain best-effort', () => {
  let attempts = 0
  const peer = {
    close() {
      attempts += 1
      throw new Error('transport already failed')
    },
  }

  assert.doesNotThrow(() => closePeer(peer as never, 1001, 'bridge shutting down'))
  assert.equal(attempts, 2)
})

test('rejects missing credentials and non-loopback bind addresses', async () => {
  await assert.rejects(startAvatarBridgeServer({ expectedToken: '', adapter: adapter() }), /token/)
  await assert.rejects(startAvatarBridgeServer({
    expectedToken: 'bridge-token',
    adapter: adapter(),
    host: '0.0.0.0' as '127.0.0.1',
  }), /loopback/)
})

test('environment startup stays disabled unless a non-empty token is injected', async () => {
  assert.equal(await startAvatarBridgeServerFromEnvironment({ environment: {}, adapter: adapter() }), undefined)
  const server = await startAvatarBridgeServerFromEnvironment({
    environment: { COMPANION_AVATAR_TOKEN: ' bridge-token ' },
    adapter: adapter(),
    port: await availablePort(),
  })
  assert.ok(server)
  await server.close()
})
