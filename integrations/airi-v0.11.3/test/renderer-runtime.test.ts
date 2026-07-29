import assert from 'node:assert/strict'
import test from 'node:test'

import type { AvatarStateSnapshot, RendererRuntimeDependencies } from '../src/renderer-runtime.ts'

import { AvatarRendererRuntime } from '../src/renderer-runtime.ts'

const state: AvatarStateSnapshot = {
  expression: {
    expression_id: 'happy', intensity: 0.8, mouth_open: 0, eye_open: 1, brow_raise: 0.2, cheek_raise: 0.3,
  },
  pose: {
    pose_id: 'idle_standing', gesture_id: null, gesture_intensity: 0.5, breathing_amplitude: 0.3,
  },
  eyes: { gaze_target: 'user', blink_rate: 1, eye_contact_duration: 3, pupil_dilation: 0.5 },
  valence: 0.7,
  arousal: 0.6,
  energy: 0.5,
  is_speaking: false,
  audio_level: 0,
}

function dependencies(overrides: Partial<RendererRuntimeDependencies> = {}): RendererRuntimeDependencies {
  return {
    listModels: async () => [
      { id: 'kurisu', name: 'Kurisu', renderer: 'live2d', expressions: ['happy', 'neutral'] },
      { id: 'legacy', name: 'Legacy', renderer: 'unsupported' },
    ],
    selectModel: async () => {},
    applyState: async () => {},
    applyExpression: async () => {},
    applyGesture: async () => {},
    applyProactiveLevel: async () => {},
    isWindowVisible: () => true,
    modelLoadTimeoutMs: 50,
    ...overrides,
  }
}

test('exposes privacy-safe models and rejects unsupported model formats', async () => {
  const runtime = new AvatarRendererRuntime(dependencies())
  const listed = await runtime.handle({ method: 'model.list', params: {} })
  const models = listed.models as Array<Record<string, unknown>>
  assert.equal(models[0]?.path, '')
  assert.deepEqual(await runtime.handle({ method: 'model.validate', params: { model_id: 'legacy' } }), {
    errors: ['unsupported model format'],
  })
  assert.deepEqual(await runtime.handle({ method: 'model.load', params: { model_id: 'missing' } }), {
    loaded: false,
  })
})

test('waits for the real model-loaded hook before acknowledging load', async () => {
  let runtime!: AvatarRendererRuntime
  runtime = new AvatarRendererRuntime(dependencies({
    selectModel: async modelId => queueMicrotask(() => runtime.notifyModelLoaded('live2d', modelId)),
  }))
  runtime.notifyStageMounted('live2d')
  assert.deepEqual(await runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } }), { loaded: true })
})

test('advances rendered state only from a presented-frame hook', async () => {
  let runtime!: AvatarRendererRuntime
  runtime = new AvatarRendererRuntime(dependencies({
    selectModel: async modelId => queueMicrotask(() => runtime.notifyModelLoaded('live2d', modelId)),
  }))
  runtime.notifyStageMounted('live2d')
  await runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } })

  await runtime.handle({ method: 'state.update', params: { state } })
  let inspection = await runtime.handle({ method: 'stage.inspect', params: {} })
  assert.equal(inspection.state_sequence, 1)
  assert.equal(inspection.rendered_state_sequence, 0)
  assert.equal(inspection.frame_sequence, 0)

  runtime.notifyPresentedFrame()
  inspection = await runtime.handle({ method: 'stage.inspect', params: {} })
  assert.equal(inspection.rendered_state_sequence, 1)
  assert.equal(inspection.frame_sequence, 1)
  assert.equal(inspection.visible, true)
})

test('increments command evidence only after renderer callbacks succeed', async () => {
  let runtime!: AvatarRendererRuntime
  runtime = new AvatarRendererRuntime(dependencies({
    selectModel: async modelId => queueMicrotask(() => runtime.notifyModelLoaded('live2d', modelId)),
    applyGesture: async () => { throw new Error('motion missing') },
  }))
  runtime.notifyStageMounted('live2d')
  await runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } })
  await runtime.handle({
    method: 'expression.trigger', params: { expression_id: 'happy', intensity: 0.7, duration_ms: 1000 },
  })
  await assert.rejects(runtime.handle({
    method: 'gesture.trigger', params: { gesture_id: 'nod', intensity: 0.5 },
  }))
  await runtime.handle({ method: 'proactive.set_level', params: { level: 2 } })

  const inspection = await runtime.handle({ method: 'stage.inspect', params: {} })
  assert.equal(inspection.expression_sequence, 1)
  assert.equal(inspection.gesture_sequence, 0)
  assert.equal(inspection.proactive_sequence, 1)
})

test('rejects malformed state before reaching renderer callbacks', async () => {
  let applied = false
  let runtime!: AvatarRendererRuntime
  runtime = new AvatarRendererRuntime(dependencies({
    selectModel: async modelId => queueMicrotask(() => runtime.notifyModelLoaded('live2d', modelId)),
    applyState: async () => { applied = true },
  }))
  runtime.notifyStageMounted('live2d')
  await runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } })
  await assert.rejects(runtime.handle({
    method: 'state.update', params: { state: { ...state, valence: 2 } },
  }))
  assert.equal(applied, false)
})

test('rejects state and commands before the renderer model is ready', async () => {
  const runtime = new AvatarRendererRuntime(dependencies())
  await assert.rejects(runtime.handle({ method: 'state.update', params: { state } }), /not ready/)
  await assert.rejects(runtime.handle({
    method: 'expression.trigger', params: { expression_id: 'happy', intensity: 0.5, duration_ms: 1000 },
  }), /not ready/)
})

test('rejects unknown method and nested state fields', async () => {
  let runtime!: AvatarRendererRuntime
  runtime = new AvatarRendererRuntime(dependencies({
    selectModel: async modelId => queueMicrotask(() => runtime.notifyModelLoaded('live2d', modelId)),
  }))
  runtime.notifyStageMounted('live2d')
  await runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } })
  await assert.rejects(runtime.handle({
    method: 'state.update',
    params: { state: { ...state, expression: { ...state.expression, typo: true } } },
  }), /unknown field/)
  await assert.rejects(runtime.handle({
    method: 'gesture.trigger', params: { gesture_id: 'nod', intensity: 0.5, typo: true },
  }), /unknown field/)
})

test('does not acknowledge a superseded concurrent model load', async () => {
  let runtime!: AvatarRendererRuntime
  const selected: string[] = []
  runtime = new AvatarRendererRuntime(dependencies({
    listModels: async () => [
      { id: 'kurisu', name: 'Kurisu', renderer: 'live2d' },
      { id: 'mayuri', name: 'Mayuri', renderer: 'live2d' },
    ],
    selectModel: async (modelId) => {
      selected.push(modelId)
      if (modelId === 'mayuri')
        queueMicrotask(() => runtime.notifyModelLoaded('live2d', 'mayuri'))
    },
  }))
  runtime.notifyStageMounted('live2d')
  const first = runtime.handle({ method: 'model.load', params: { model_id: 'kurisu' } })
  const second = runtime.handle({ method: 'model.load', params: { model_id: 'mayuri' } })
  assert.deepEqual(await second, { loaded: true })
  assert.deepEqual(await first, { loaded: false })
  assert.deepEqual(selected, ['kurisu', 'mayuri'])
})
