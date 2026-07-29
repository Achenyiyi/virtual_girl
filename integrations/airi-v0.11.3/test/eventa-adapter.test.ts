import assert from 'node:assert/strict'
import test from 'node:test'

import { createEventaStageAdapter } from '../src/eventa-adapter.ts'

test('forwards every stage operation through the renderer invoke boundary', async () => {
  const calls: Array<{ method: string, params: Record<string, unknown> }> = []
  const adapter = createEventaStageAdapter(async (request) => {
    calls.push(request)
    return { method: request.method }
  })

  await adapter.health()
  await adapter.listModels()
  await adapter.validateModel({ model_id: 'kurisu' })
  await adapter.loadModel({ model_id: 'kurisu' })
  await adapter.updateState({ state: {} })
  await adapter.triggerExpression({ expression_id: 'happy' })
  await adapter.triggerGesture({ gesture_id: 'nod' })
  await adapter.setProactiveLevel({ level: 2 })
  await adapter.inspectStage()

  assert.deepEqual(calls.map(call => call.method), [
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
  assert.deepEqual(calls[2]?.params, { model_id: 'kurisu' })
})

test('combines renderer visibility with the real Electron main-window state', async () => {
  let windowVisible = false
  const adapter = createEventaStageAdapter(
    async request => request.method === 'stage.inspect' ? { visible: true } : {},
    () => windowVisible,
  )
  assert.equal((await adapter.inspectStage()).visible, false)
  windowVisible = true
  assert.equal((await adapter.inspectStage()).visible, true)
})
