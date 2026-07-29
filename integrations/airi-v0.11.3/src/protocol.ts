import { createHash, timingSafeEqual } from 'node:crypto'

export const PROTOCOL_NAME = 'companion-avatar'
export const PROTOCOL_VERSION = 1

const METHODS = new Set([
  'expression.trigger',
  'gesture.trigger',
  'handshake',
  'health',
  'model.list',
  'model.load',
  'model.validate',
  'proactive.set_level',
  'stage.inspect',
  'state.update',
])

export interface AvatarBridgeRequest {
  protocol: typeof PROTOCOL_NAME
  version: typeof PROTOCOL_VERSION
  type: 'request'
  id: string
  method: string
  params: Record<string, unknown>
}

export type AvatarBridgeResponse
  = | {
    protocol: typeof PROTOCOL_NAME
    version: typeof PROTOCOL_VERSION
    type: 'response'
    id: string
    ok: true
    result: Record<string, unknown>
  }
  | {
    protocol: typeof PROTOCOL_NAME
    version: typeof PROTOCOL_VERSION
    type: 'response'
    id: string
    ok: false
    error: string
  }

export interface AvatarStageAdapter {
  health(): Promise<Record<string, unknown>>
  listModels(): Promise<Record<string, unknown>>
  validateModel(params: Record<string, unknown>): Promise<Record<string, unknown>>
  loadModel(params: Record<string, unknown>): Promise<Record<string, unknown>>
  updateState(params: Record<string, unknown>): Promise<Record<string, unknown>>
  triggerExpression(params: Record<string, unknown>): Promise<Record<string, unknown>>
  triggerGesture(params: Record<string, unknown>): Promise<Record<string, unknown>>
  setProactiveLevel(params: Record<string, unknown>): Promise<Record<string, unknown>>
  inspectStage(): Promise<Record<string, unknown>>
}

export interface AvatarBridgeSession {
  authenticated: boolean
}

export function createAvatarBridgeSession(): AvatarBridgeSession {
  return { authenticated: false }
}

export function parseAvatarBridgeRequest(raw: string, maxMessageBytes: number): AvatarBridgeRequest {
  if (Buffer.byteLength(raw, 'utf8') > maxMessageBytes)
    throw new ProtocolError('request exceeds configured message limit')

  let value: unknown
  try {
    value = JSON.parse(raw)
  }
  catch {
    throw new ProtocolError('request must be valid JSON')
  }
  if (!isRecord(value))
    throw new ProtocolError('request must be an object')
  if (value.protocol !== PROTOCOL_NAME || value.version !== PROTOCOL_VERSION || value.type !== 'request')
    throw new ProtocolError('request envelope is invalid')
  if (typeof value.id !== 'string' || !value.id || value.id.length > 128)
    throw new ProtocolError('request id is invalid')
  if (typeof value.method !== 'string' || !METHODS.has(value.method))
    throw new ProtocolError('request method is unsupported')
  if (!isRecord(value.params))
    throw new ProtocolError('request params must be an object')
  return value as unknown as AvatarBridgeRequest
}

export async function handleAvatarBridgeRequest(options: {
  request: AvatarBridgeRequest
  session: AvatarBridgeSession
  expectedToken: string
  adapter: AvatarStageAdapter
}): Promise<AvatarBridgeResponse> {
  const { request, session, expectedToken, adapter } = options
  try {
    if (request.method === 'handshake') {
      if (session.authenticated)
        throw new ProtocolError('handshake is already complete')
      const versions = request.params.supported_versions
      const client = request.params.client
      const token = request.params.auth_token
      if (!Array.isArray(versions) || !versions.includes(PROTOCOL_VERSION))
        throw new ProtocolError('protocol version 1 is required')
      if (typeof client !== 'string' || !client || client.length > 128)
        throw new ProtocolError('client identifier is invalid')
      if (typeof token !== 'string' || !expectedToken || !timingSafeStringEqual(token, expectedToken))
        throw new ProtocolError('authentication failed')
      session.authenticated = true
      return success(request.id, { version: PROTOCOL_VERSION })
    }
    if (!session.authenticated)
      throw new ProtocolError('handshake is required')

    const handlers: Record<string, () => Promise<Record<string, unknown>>> = {
      health: () => adapter.health(),
      'model.list': () => adapter.listModels(),
      'model.validate': () => adapter.validateModel(request.params),
      'model.load': () => adapter.loadModel(request.params),
      'state.update': () => adapter.updateState(request.params),
      'expression.trigger': () => adapter.triggerExpression(request.params),
      'gesture.trigger': () => adapter.triggerGesture(request.params),
      'proactive.set_level': () => adapter.setProactiveLevel(request.params),
      'stage.inspect': () => adapter.inspectStage(),
    }
    const handler = handlers[request.method]
    if (!handler)
      throw new ProtocolError('request method is unsupported')
    try {
      return success(request.id, await handler())
    }
    catch {
      return failure(request.id, 'stage operation failed')
    }
  }
  catch (error) {
    return failure(request.id, error instanceof ProtocolError ? error.message : 'request failed')
  }
}

export class ProtocolError extends Error {}

function success(id: string, result: Record<string, unknown>): AvatarBridgeResponse {
  return { protocol: PROTOCOL_NAME, version: PROTOCOL_VERSION, type: 'response', id, ok: true, result }
}

function failure(id: string, error: string): AvatarBridgeResponse {
  return { protocol: PROTOCOL_NAME, version: PROTOCOL_VERSION, type: 'response', id, ok: false, error }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function timingSafeStringEqual(left: string, right: string): boolean {
  const leftDigest = createHash('sha256').update(left, 'utf8').digest()
  const rightDigest = createHash('sha256').update(right, 'utf8').digest()
  return timingSafeEqual(leftDigest, rightDigest)
}
