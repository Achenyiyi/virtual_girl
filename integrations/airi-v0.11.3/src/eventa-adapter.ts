import type { AvatarStageAdapter } from './protocol.ts'
import type { AvatarBridgeMethod, AvatarRendererInvoke } from './renderer-contract.ts'

export function createEventaStageAdapter(invokeRenderer: AvatarRendererInvoke): AvatarStageAdapter {
  const invoke = (method: Exclude<AvatarBridgeMethod, 'handshake'>, params: Record<string, unknown> = {}) =>
    invokeRenderer({ method, params })

  return {
    health: () => invoke('health'),
    listModels: () => invoke('model.list'),
    validateModel: params => invoke('model.validate', params),
    loadModel: params => invoke('model.load', params),
    updateState: params => invoke('state.update', params),
    triggerExpression: params => invoke('expression.trigger', params),
    triggerGesture: params => invoke('gesture.trigger', params),
    setProactiveLevel: params => invoke('proactive.set_level', params),
    inspectStage: () => invoke('stage.inspect'),
  }
}
