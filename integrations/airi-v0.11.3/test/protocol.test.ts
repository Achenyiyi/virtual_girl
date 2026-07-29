import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createAvatarBridgeSession,
  handleAvatarBridgeRequest,
  parseAvatarBridgeRequest,
  ProtocolError,
  type AvatarStageAdapter,
} from '../src/protocol.ts'

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

function request(method: string, params: Record<string, unknown> = {}) {
  return parseAvatarBridgeRequest(JSON.stringify({
    protocol: 'companion-avatar',
    version: 1,
    type: 'request',
    id: `request-${method}`,
    method,
    params,
  }), 4096)
}

test('rejects malformed, oversized, and unsupported requests', () => {
  assert.throws(() => parseAvatarBridgeRequest('{', 4096), ProtocolError)
  assert.throws(() => parseAvatarBridgeRequest('x'.repeat(4097), 4096), ProtocolError)
  assert.throws(() => request('unknown.method'), ProtocolError)
})

test('requires an authenticated version-1 handshake before stage calls', async () => {
  const session = createAvatarBridgeSession()
  const beforeHandshake = await handleAvatarBridgeRequest({
    request: request('health'),
    session,
    expectedToken: 'expected-token',
    adapter: adapter(),
  })
  assert.equal(beforeHandshake.ok, false)
  assert.equal('error' in beforeHandshake && beforeHandshake.error, 'handshake is required')

  const wrongToken = await handleAvatarBridgeRequest({
    request: request('handshake', {
      supported_versions: [1], client: 'virtual-companion', auth_token: 'wrong-token',
    }),
    session,
    expectedToken: 'expected-token',
    adapter: adapter(),
  })
  assert.equal(wrongToken.ok, false)
  assert.equal(session.authenticated, false)

  const missingClient = await handleAvatarBridgeRequest({
    request: request('handshake', { supported_versions: [1], auth_token: 'expected-token' }),
    session,
    expectedToken: 'expected-token',
    adapter: adapter(),
  })
  assert.equal(missingClient.ok, false)
  assert.equal(session.authenticated, false)

  const accepted = await handleAvatarBridgeRequest({
    request: request('handshake', {
      supported_versions: [1], client: 'virtual-companion', auth_token: 'expected-token',
    }),
    session,
    expectedToken: 'expected-token',
    adapter: adapter(),
  })
  assert.equal(accepted.ok, true)
  assert.equal(session.authenticated, true)

  const duplicate = await handleAvatarBridgeRequest({
    request: request('handshake', {
      supported_versions: [1], client: 'virtual-companion', auth_token: 'expected-token',
    }),
    session,
    expectedToken: 'expected-token',
    adapter: adapter(),
  })
  assert.equal(duplicate.ok, false)
  assert.equal('error' in duplicate && duplicate.error, 'handshake is already complete')
})

test('routes every renderer operation after handshake', async () => {
  const calls: string[] = []
  const stage = adapter({
    health: async () => { calls.push('health'); return { status: 'healthy' } },
    listModels: async () => { calls.push('model.list'); return { models: [] } },
    validateModel: async () => { calls.push('model.validate'); return { errors: [] } },
    loadModel: async () => { calls.push('model.load'); return { loaded: true } },
    updateState: async () => { calls.push('state.update'); return {} },
    triggerExpression: async () => { calls.push('expression.trigger'); return {} },
    triggerGesture: async () => { calls.push('gesture.trigger'); return {} },
    setProactiveLevel: async () => { calls.push('proactive.set_level'); return {} },
    inspectStage: async () => { calls.push('stage.inspect'); return { renderer: 'live2d' } },
  })
  const session = { authenticated: true }
  for (const method of [
    'health',
    'model.list',
    'model.validate',
    'model.load',
    'state.update',
    'expression.trigger',
    'gesture.trigger',
    'proactive.set_level',
    'stage.inspect',
  ]) {
    const response = await handleAvatarBridgeRequest({
      request: request(method),
      session,
      expectedToken: 'expected-token',
      adapter: stage,
    })
    assert.equal(response.ok, true)
  }
  assert.deepEqual(calls, [
    'health',
    'model.list',
    'model.validate',
    'model.load',
    'state.update',
    'expression.trigger',
    'gesture.trigger',
    'proactive.set_level',
    'stage.inspect',
  ])
})

test('does not expose any adapter exception text', async () => {
  for (const error of [new Error('private model path'), new ProtocolError('private protocol detail')]) {
    const response = await handleAvatarBridgeRequest({
      request: request('stage.inspect'),
      session: { authenticated: true },
      expectedToken: 'expected-token',
      adapter: adapter({ inspectStage: async () => { throw error } }),
    })
    assert.equal(response.ok, false)
    assert.equal('error' in response && response.error, 'stage operation failed')
  }
})
