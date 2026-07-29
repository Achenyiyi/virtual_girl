import assert from 'node:assert/strict'
import test from 'node:test'

import {
  createSemanticExpressionController,
  requireLive2DMotionStarted,
  resolveLive2DGesture,
} from '../src/live2d-avatar-semantics.ts'

function coreModel(parameterIds: string[]) {
  const values = new Map<string, number>()
  const parameters = {
    ids: parameterIds,
    minimumValues: parameterIds.map(id => id === 'ParamMouthForm' ? -1 : 0),
    maximumValues: parameterIds.map(() => 1),
    defaultValues: parameterIds.map(() => 0),
  }
  return {
    getModel: () => ({ parameters }),
    setParameterValueById(parameterId: string, value: number) {
      values.set(parameterId, value)
    },
    values,
  }
}

test('applies synthetic happy parameters as a final per-frame override', () => {
  const model = coreModel(['ParamEyeLSmile', 'ParamEyeRSmile', 'ParamMouthForm', 'ParamCheek'])
  const controller = createSemanticExpressionController()

  assert.equal(controller.set(model, 'happy', 0.75, 0), true)
  model.values.set('ParamMouthForm', -1)
  controller.apply(model)

  assert.equal(model.values.get('ParamEyeLSmile'), 0.75)
  assert.equal(model.values.get('ParamEyeRSmile'), 0.75)
  assert.equal(model.values.get('ParamMouthForm'), 0.75)
  assert.ok(Math.abs((model.values.get('ParamCheek') ?? 0) - 0.6) < 1e-12)
})

test('resets synthetic parameters when neutral is requested', () => {
  const model = coreModel(['ParamEyeLSmile', 'ParamMouthForm'])
  const controller = createSemanticExpressionController()
  controller.set(model, 'happy', 1, 0)
  controller.apply(model)

  assert.equal(controller.set(model, 'neutral', 1, 0), true)
  controller.apply(model)

  assert.equal(model.values.get('ParamEyeLSmile'), 0)
  assert.equal(model.values.get('ParamMouthForm'), 0)
})

test('fails closed when an expression has no real model parameters', () => {
  const model = coreModel(['ParamAngleX'])
  const controller = createSemanticExpressionController()

  assert.equal(controller.set(model, 'happy', 1, 0), false)
  assert.equal(controller.set(model, 'unknown', 1, 0), false)
})

test('requires a mouth-form parameter plus another visible expression parameter', () => {
  const controller = createSemanticExpressionController()

  assert.equal(controller.set(coreModel(['ParamMouthForm']), 'happy', 1, 0), false)
  assert.equal(controller.set(coreModel(['ParamBrowLY']), 'happy', 1, 0), false)
})

test('an unknown expression does not cancel the active reset timer', async () => {
  const model = coreModel(['ParamEyeLSmile', 'ParamMouthForm'])
  const controller = createSemanticExpressionController()

  assert.equal(controller.set(model, 'happy', 1, 1), true)
  controller.apply(model)
  assert.equal(controller.set(model, 'unknown', 1, 0), false)
  await new Promise(resolve => setTimeout(resolve, 10))
  controller.apply(model)

  assert.equal(model.values.get('ParamEyeLSmile'), 0)
  assert.equal(model.values.get('ParamMouthForm'), 0)
})

test('maps the acceptance nod to Hiyori FlickDown', () => {
  const motion = resolveLive2DGesture('nod', [
    { motionName: 'Idle', motionIndex: 0, fileName: 'motion/hiyori_m01.motion3.json' },
    { motionName: 'FlickDown', motionIndex: 0, fileName: 'motion/hiyori_m04.motion3.json' },
  ], 'preset-live2d-1')

  assert.deepEqual(motion, {
    motionName: 'FlickDown',
    motionIndex: 0,
    fileName: 'motion/hiyori_m04.motion3.json',
  })
})

test('does not invent aliases for unverified Hiyori motions', () => {
  const motions = [
    { motionName: 'FlickUp', motionIndex: 0, fileName: 'motion/hiyori_m03.motion3.json' },
    { motionName: 'Flick', motionIndex: 0, fileName: 'motion/hiyori_m05.motion3.json' },
  ]

  assert.equal(resolveLive2DGesture('wave', motions, 'preset-live2d-1'), undefined)
  assert.equal(resolveLive2DGesture('head_tilt', motions, 'preset-live2d-1'), undefined)
})

test('does not reuse the Hiyori nod alias for another model or motion file', () => {
  const hiyoriMotion = [
    { motionName: 'FlickDown', motionIndex: 0, fileName: 'motion/hiyori_m04.motion3.json' },
  ]
  const otherMotion = [
    { motionName: 'FlickDown', motionIndex: 0, fileName: 'motion/other_m04.motion3.json' },
  ]

  assert.equal(resolveLive2DGesture('nod', hiyoriMotion, 'display-model-custom'), undefined)
  assert.equal(resolveLive2DGesture('nod', otherMotion, 'preset-live2d-1'), undefined)
})

test('rejects motion acknowledgement when the SDK reports no start', async () => {
  await assert.rejects(
    requireLive2DMotionStarted(async () => false, 'FlickDown'),
    /did not start/,
  )
})
