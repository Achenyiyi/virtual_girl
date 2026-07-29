import type { Peer } from 'crossws'

import { plugin as crossWsPlugin } from 'crossws/server'
import { defineWebSocketHandler, H3, serve } from 'h3'

import type { AvatarStageAdapter } from './protocol.ts'

import {
  createAvatarBridgeSession,
  handleAvatarBridgeRequest,
  parseAvatarBridgeRequest,
} from './protocol.ts'

export interface AvatarBridgeServerOptions {
  expectedToken: string
  adapter: AvatarStageAdapter
  host?: '127.0.0.1' | '::1'
  port?: number
  maxMessageBytes?: number
  maxConcurrentRequestsPerConnection?: number
}

export interface AvatarBridgeServer {
  host: string
  port: number
  url: string
  close(): Promise<void>
}

export interface AvatarBridgeEnvironment {
  COMPANION_AVATAR_TOKEN?: string
}

const CLOSE_POLICY_VIOLATION = 1008
const CLOSE_MESSAGE_TOO_BIG = 1009
const CLOSE_INTERNAL_ERROR = 1011

export async function startAvatarBridgeServer(options: AvatarBridgeServerOptions): Promise<AvatarBridgeServer> {
  if (!options.expectedToken)
    throw new Error('avatar bridge token is required')

  const host = options.host ?? '127.0.0.1'
  const port = options.port ?? 6121
  const maxMessageBytes = options.maxMessageBytes ?? 1_048_576
  const maxConcurrentRequests = options.maxConcurrentRequestsPerConnection ?? 32
  if (host !== '127.0.0.1' && host !== '::1')
    throw new Error('avatar bridge must bind to a loopback address')
  if (!Number.isSafeInteger(port) || port < 1 || port > 65_535)
    throw new Error('avatar bridge port is invalid')
  if (!Number.isSafeInteger(maxMessageBytes) || maxMessageBytes < 1024)
    throw new Error('avatar bridge message limit is invalid')
  if (!Number.isSafeInteger(maxConcurrentRequests) || maxConcurrentRequests < 1 || maxConcurrentRequests > 256)
    throw new Error('avatar bridge concurrency limit is invalid')

  const sessions = new Map<string, { authenticated: boolean, activeRequests: number }>()
  const peers = new Map<string, Peer>()
  const app = new H3()
  app.get('/ws', defineWebSocketHandler({
    open(peer) {
      peers.set(peer.id, peer)
      sessions.set(peer.id, { ...createAvatarBridgeSession(), activeRequests: 0 })
    },
    async message(peer, message) {
      const session = sessions.get(peer.id)
      if (!session) {
        closePeer(peer, CLOSE_INTERNAL_ERROR, 'session unavailable')
        return
      }
      if (session.activeRequests >= maxConcurrentRequests) {
        closePeer(peer, CLOSE_POLICY_VIOLATION, 'too many concurrent requests')
        return
      }

      let request
      try {
        if (rawMessageBytes(message.rawData) > maxMessageBytes) {
          closePeer(peer, CLOSE_MESSAGE_TOO_BIG, 'request exceeds message limit')
          return
        }
        request = parseAvatarBridgeRequest(message.text(), maxMessageBytes)
      }
      catch (error) {
        const tooLarge = error instanceof Error && error.message.includes('message limit')
        closePeer(peer, tooLarge ? CLOSE_MESSAGE_TOO_BIG : CLOSE_POLICY_VIOLATION, 'invalid request')
        return
      }

      session.activeRequests += 1
      try {
        const response = await handleAvatarBridgeRequest({
          request,
          session,
          expectedToken: options.expectedToken,
          adapter: options.adapter,
        })
        const encoded = JSON.stringify(response)
        if (Buffer.byteLength(encoded, 'utf8') > maxMessageBytes) {
          closePeer(peer, CLOSE_MESSAGE_TOO_BIG, 'response exceeds message limit')
          return
        }
        peer.send(encoded)
        if (!session.authenticated)
          closePeer(peer, CLOSE_POLICY_VIOLATION, 'authentication required')
      }
      catch {
        closePeer(peer, CLOSE_INTERNAL_ERROR, 'bridge operation failed')
      }
      finally {
        session.activeRequests -= 1
      }
    },
    close(peer) {
      peers.delete(peer.id)
      sessions.delete(peer.id)
    },
    error(peer) {
      peers.delete(peer.id)
      sessions.delete(peer.id)
    },
  }))

  const server = serve(app, {
    // H3's crossws response is resolved by the server plugin at runtime.
    // @ts-expect-error H3 does not expose the crossws extension on its fetch response type.
    plugins: [crossWsPlugin({ resolve: async request => (await app.fetch(request)).crossws })],
    hostname: host,
    port,
    manual: true,
    reusePort: false,
    silent: true,
    gracefulShutdown: { forceTimeout: 0.25, gracefulTimeout: 0.25 },
  })
  await server.serve()

  let closed = false
  return {
    host,
    port,
    url: `ws://${host === '::1' ? `[${host}]` : host}:${port}/ws`,
    async close() {
      if (closed)
        return
      closed = true
      for (const peer of peers.values())
        closePeer(peer, 1001, 'bridge shutting down')
      peers.clear()
      sessions.clear()
      await server.close()
    },
  }
}

export async function startAvatarBridgeServerFromEnvironment(options: {
  environment: AvatarBridgeEnvironment
  adapter: AvatarStageAdapter
  host?: '127.0.0.1' | '::1'
  port?: number
  maxMessageBytes?: number
  maxConcurrentRequestsPerConnection?: number
}): Promise<AvatarBridgeServer | undefined> {
  const token = options.environment.COMPANION_AVATAR_TOKEN?.trim()
  if (!token)
    return undefined
  return await startAvatarBridgeServer({
    ...options,
    expectedToken: token,
  })
}

export function closePeer(peer: Peer, code: number, reason: string): void {
  try {
    peer.close(code, reason)
  }
  catch {
    try {
      peer.close()
    }
    catch {
      // Closing a broken transport is best-effort; callers must keep shutting down.
    }
  }
}

function rawMessageBytes(value: unknown): number {
  if (typeof value === 'string')
    return Buffer.byteLength(value, 'utf8')
  if (value instanceof ArrayBuffer || value instanceof SharedArrayBuffer)
    return value.byteLength
  if (ArrayBuffer.isView(value))
    return value.byteLength
  if (value instanceof Blob)
    return value.size
  return Number.MAX_SAFE_INTEGER
}
