export interface Live2DCoreModelParameters {
  ids: string[]
  minimumValues: ArrayLike<number>
  maximumValues: ArrayLike<number>
  defaultValues: ArrayLike<number>
}

export interface Live2DCoreModelLike {
  getModel(): { parameters: Live2DCoreModelParameters }
  setParameterValueById(parameterId: string, value: number): void
}

export interface Live2DMotionDescriptor {
  motionName: string
  motionIndex: number
  fileName: string
}

interface ActiveExpressionParameter {
  parameterId: string
  value: number
  defaultValue: number
}

interface ActiveExpression {
  coreModel: Live2DCoreModelLike
  parameters: ActiveExpressionParameter[]
}

type SemanticParameterId
  = | 'ParamEyeLSmile'
    | 'ParamEyeRSmile'
    | 'ParamMouthForm'
    | 'ParamCheek'
    | 'ParamBrowLY'
    | 'ParamBrowRY'
    | 'ParamBrowLAngle'
    | 'ParamBrowRAngle'

type SemanticExpressionTargets = Record<SemanticParameterId, number>

const SEMANTIC_EXPRESSION_TARGETS: Record<string, SemanticExpressionTargets> = {
  happy: expressionTargets({ eyeSmile: 1, mouthForm: 1, cheek: 0.8, browY: 0.25 }),
  gentle_smile: expressionTargets({ eyeSmile: 0.55, mouthForm: 0.65, cheek: 0.4, browY: 0.15 }),
  content: expressionTargets({ eyeSmile: 0.4, mouthForm: 0.45, cheek: 0.25, browY: 0.05 }),
  attentive: expressionTargets({ eyeSmile: 0.05, mouthForm: 0.1, cheek: 0, browY: 0.35 }),
  worried: expressionTargets({ eyeSmile: 0, mouthForm: -0.45, cheek: 0, browY: 0.25, browAngle: 0.25 }),
  sad: expressionTargets({ eyeSmile: 0, mouthForm: -0.75, cheek: 0, browY: -0.2, browAngle: 0.35 }),
  upset: expressionTargets({ eyeSmile: 0, mouthForm: -1, cheek: 0.1, browY: -0.4, browAngle: -0.35 }),
}

const SEMANTIC_PARAMETER_IDS = Object.keys(
  expressionTargets({ eyeSmile: 0, mouthForm: 0, cheek: 0, browY: 0 }),
) as SemanticParameterId[]

const GESTURE_MOTION_ALIASES: Record<string, ReadonlyArray<{
  group: string
  index: number
  fileName: string
  modelIds: readonly string[]
}>> = {
  nod: [{
    group: 'FlickDown',
    index: 0,
    fileName: 'hiyori_m04',
    modelIds: ['preset-live2d-1', 'preset-live2d-2'],
  }],
}

function expressionTargets(values: {
  eyeSmile: number
  mouthForm: number
  cheek: number
  browY: number
  browAngle?: number
}): SemanticExpressionTargets {
  return {
    ParamEyeLSmile: values.eyeSmile,
    ParamEyeRSmile: values.eyeSmile,
    ParamMouthForm: values.mouthForm,
    ParamCheek: values.cheek,
    ParamBrowLY: values.browY,
    ParamBrowRY: values.browY,
    ParamBrowLAngle: values.browAngle ?? 0,
    ParamBrowRAngle: -(values.browAngle ?? 0),
  }
}

function normalizeSemanticId(value: string): string {
  return value.trim().toLowerCase().replace(/[\s-]+/g, '_')
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value))
}

function supportedParameterIndices(coreModel: Live2DCoreModelLike): Map<string, number> {
  const ids = coreModel.getModel().parameters.ids
  return new Map(ids.map((id, index) => [id, index]))
}

function supportsSemanticExpression(parameterIndices: ReadonlyMap<string, number>): boolean {
  return parameterIndices.has('ParamMouthForm')
    && ['ParamEyeLSmile', 'ParamEyeRSmile', 'ParamCheek', 'ParamBrowLY', 'ParamBrowRY']
      .some(id => parameterIndices.has(id))
}

function motionFileStem(fileName: string): string {
  return fileName.split(/[\\/]/).pop()?.replace(/\.motion3\.json$/i, '') ?? ''
}

export function createSemanticExpressionController() {
  let active: ActiveExpression | undefined
  let pendingReset: ActiveExpression | undefined
  let resetTimer: ReturnType<typeof setTimeout> | undefined

  function clearTimer() {
    if (resetTimer !== undefined) {
      clearTimeout(resetTimer)
      resetTimer = undefined
    }
  }

  function queueReset() {
    if (active)
      pendingReset = active
    active = undefined
  }

  function set(coreModel: Live2DCoreModelLike, expressionId: string, intensity: number, durationMs: number): boolean {
    if (!Number.isFinite(intensity) || intensity < 0 || intensity > 1 || !Number.isSafeInteger(durationMs) || durationMs < 0)
      return false

    const normalized = normalizeSemanticId(expressionId)
    const parameterIndices = supportedParameterIndices(coreModel)
    if (!supportsSemanticExpression(parameterIndices))
      return false

    if (normalized === 'neutral') {
      clearTimer()
      queueReset()
      return true
    }

    const targets = SEMANTIC_EXPRESSION_TARGETS[normalized]
    if (!targets)
      return false

    clearTimer()
    const modelParameters = coreModel.getModel().parameters
    const parameters: ActiveExpressionParameter[] = []
    for (const parameterId of SEMANTIC_PARAMETER_IDS) {
      const index = parameterIndices.get(parameterId)
      if (index === undefined)
        continue

      const defaultValue = Number(modelParameters.defaultValues[index] ?? 0)
      const targetValue = clamp(
        targets[parameterId],
        Number(modelParameters.minimumValues[index] ?? targets[parameterId]),
        Number(modelParameters.maximumValues[index] ?? targets[parameterId]),
      )
      parameters.push({
        parameterId,
        value: defaultValue + (targetValue - defaultValue) * intensity,
        defaultValue,
      })
    }
    if (parameters.length === 0)
      return false

    pendingReset = undefined
    const nextActive: ActiveExpression = { coreModel, parameters }
    active = nextActive
    if (durationMs > 0) {
      resetTimer = setTimeout(() => {
        if (active === nextActive)
          queueReset()
        resetTimer = undefined
      }, durationMs)
    }
    return true
  }

  function apply(coreModel: Live2DCoreModelLike) {
    if (pendingReset?.coreModel === coreModel) {
      for (const parameter of pendingReset.parameters)
        coreModel.setParameterValueById(parameter.parameterId, parameter.defaultValue)
      pendingReset = undefined
    }

    if (active?.coreModel !== coreModel)
      return
    for (const parameter of active.parameters)
      coreModel.setParameterValueById(parameter.parameterId, parameter.value)
  }

  function clear() {
    clearTimer()
    queueReset()
  }

  function dispose() {
    clearTimer()
    active = undefined
    pendingReset = undefined
  }

  return { set, apply, clear, dispose }
}

export function resolveLive2DGesture(
  gestureId: string,
  availableMotions: readonly Live2DMotionDescriptor[],
  modelId: string,
): Live2DMotionDescriptor | undefined {
  const normalized = normalizeSemanticId(gestureId)
  const exact = availableMotions.find((motion) => {
    const fileName = motionFileStem(motion.fileName)
    return normalizeSemanticId(motion.motionName) === normalized || normalizeSemanticId(fileName) === normalized
  })
  if (exact)
    return exact

  for (const alias of GESTURE_MOTION_ALIASES[normalized] ?? []) {
    const matched = availableMotions.find(motion =>
      alias.modelIds.includes(modelId)
      && motion.motionIndex === alias.index
      && motion.motionName.toLowerCase() === alias.group.toLowerCase()
      && normalizeSemanticId(motionFileStem(motion.fileName)) === normalizeSemanticId(alias.fileName),
    )
    if (matched)
      return matched
  }
  return undefined
}

export async function requireLive2DMotionStarted(
  startMotion: () => Promise<boolean>,
  motionName: string,
): Promise<void> {
  if (!await startMotion())
    throw new Error(`Live2D motion did not start: ${motionName}`)
}
