export const AVATAR_RENDERER_INVOKE_ID = 'eventa:invoke:electron:avatar-bridge:renderer'

export interface AvatarRendererRequest {
  method: Exclude<AvatarBridgeMethod, 'handshake'>
  params: Record<string, unknown>
}

export type AvatarBridgeMethod
  = | 'expression.trigger'
    | 'gesture.trigger'
    | 'handshake'
    | 'health'
    | 'model.list'
    | 'model.load'
    | 'model.validate'
    | 'proactive.set_level'
    | 'stage.inspect'
    | 'state.update'

export type AvatarRendererInvoke = (
  request: AvatarRendererRequest,
) => Promise<Record<string, unknown>>
