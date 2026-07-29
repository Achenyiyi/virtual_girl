export type SupportedRenderer = 'live2d' | 'vrm'

export interface RendererModel {
  id: string
  name: string
  renderer: SupportedRenderer | 'unsupported'
  expressions?: string[]
}

export interface RendererRuntimeDependencies {
  listModels(): Promise<RendererModel[]>
  selectModel(modelId: string): Promise<void>
  applyState(state: AvatarStateSnapshot): Promise<void>
  applyExpression(expressionId: string, intensity: number, durationMs: number): Promise<void>
  applyGesture(gestureId: string, intensity: number): Promise<void>
  applyProactiveLevel(level: number): Promise<void>
  isWindowVisible(): boolean
  modelLoadTimeoutMs?: number
}

export interface AvatarStateSnapshot {
  expression: {
    expression_id: string
    intensity: number
    mouth_open: number
    eye_open: number
    brow_raise: number
    cheek_raise: number
  }
  pose: {
    pose_id: string
    gesture_id: string | null
    gesture_intensity: number
    breathing_amplitude: number
  }
  eyes: {
    gaze_target: string
    blink_rate: number
    eye_contact_duration: number
    pupil_dilation: number
  }
  valence: number
  arousal: number
  energy: number
  is_speaking: boolean
  audio_level: number
}

interface RendererState {
  renderer: SupportedRenderer | ''
  modelId: string
  modelLoaded: boolean
  stageMounted: boolean
  stateSequence: number
  renderedStateSequence: number
  frameSequence: number
  expressionSequence: number
  gestureSequence: number
  proactiveSequence: number
  expressionId: string
  valence: number
  arousal: number
  proactiveLevel: number
  lastGestureId: string
}

export class AvatarRendererRuntime {
  private readonly dependencies: RendererRuntimeDependencies
  private readonly state: RendererState = {
    renderer: '',
    modelId: '',
    modelLoaded: false,
    stageMounted: false,
    stateSequence: 0,
    renderedStateSequence: 0,
    frameSequence: 0,
    expressionSequence: 0,
    gestureSequence: 0,
    proactiveSequence: 0,
    expressionId: 'neutral',
    valence: 0,
    arousal: 0.5,
    proactiveLevel: 0,
    lastGestureId: '',
  }

  private readonly modelWaiters = new Set<() => void>()
  private modelLoadGeneration = 0
  private activeModelLoad?: { modelId: string, renderer: SupportedRenderer }
  private pendingModelLoad?: { generation: number, modelId: string, renderer: SupportedRenderer }

  constructor(dependencies: RendererRuntimeDependencies) {
    this.dependencies = dependencies
  }

  async handle(request: { method: string, params: Record<string, unknown> }): Promise<Record<string, unknown>> {
    switch (request.method) {
      case 'health': return this.health()
      case 'model.list': return await this.listModels()
      case 'model.validate': return await this.validateModel(request.params)
      case 'model.load': return await this.loadModel(request.params)
      case 'state.update': return await this.updateState(request.params)
      case 'expression.trigger': return await this.triggerExpression(request.params)
      case 'gesture.trigger': return await this.triggerGesture(request.params)
      case 'proactive.set_level': return await this.setProactiveLevel(request.params)
      case 'stage.inspect': return this.inspectStage()
      default: throw new Error('unsupported renderer operation')
    }
  }

  notifyStageMounted(renderer: SupportedRenderer): void {
    if (this.state.renderer && this.state.renderer !== renderer)
      this.state.modelLoaded = false
    this.state.renderer = renderer
    this.state.stageMounted = true
  }

  notifyStageUnmounted(): void {
    this.state.stageMounted = false
    this.state.modelLoaded = false
    this.activeModelLoad = undefined
    this.pendingModelLoad = undefined
    this.modelLoadGeneration += 1
    this.resolveModelWaiters()
  }

  notifyModelLoading(renderer: SupportedRenderer, modelId: string): void {
    const supersedesPending = this.pendingModelLoad
      && (renderer !== this.pendingModelLoad.renderer || modelId !== this.pendingModelLoad.modelId)

    this.activeModelLoad = { renderer, modelId }
    this.state.renderer = renderer
    this.state.modelId = modelId
    this.state.modelLoaded = false

    if (supersedesPending) {
      this.pendingModelLoad = undefined
      this.modelLoadGeneration += 1
      this.resolveModelWaiters()
    }
  }

  notifyModelLoaded(renderer: SupportedRenderer, modelId: string): void {
    if (!this.state.stageMounted || renderer !== this.state.renderer)
      return
    if (!this.activeModelLoad && !this.pendingModelLoad && this.state.modelId && modelId !== this.state.modelId)
      return
    if (this.activeModelLoad
      && (renderer !== this.activeModelLoad.renderer || modelId !== this.activeModelLoad.modelId))
      return
    if (this.pendingModelLoad
      && (renderer !== this.pendingModelLoad.renderer || modelId !== this.pendingModelLoad.modelId))
      return
    this.state.renderer = renderer
    this.state.modelId = modelId
    this.state.modelLoaded = true
    this.activeModelLoad = undefined
    this.pendingModelLoad = undefined
    this.resolveModelWaiters()
  }

  notifyPresentedFrame(): void {
    if (!this.state.stageMounted || !this.state.modelLoaded)
      return
    this.state.frameSequence += 1
    this.state.renderedStateSequence = this.state.stateSequence
  }

  private health(): Record<string, unknown> {
    return { status: this.state.stageMounted ? 'healthy' : 'unhealthy' }
  }

  private async listModels(): Promise<Record<string, unknown>> {
    const models = await this.dependencies.listModels()
    return {
      models: models.map(model => ({
        model_id: model.id,
        name: model.name,
        type: model.renderer,
        path: '',
        expressions: model.expressions ?? [],
        validation_errors: model.renderer === 'unsupported' ? ['unsupported model format'] : [],
      })),
    }
  }

  private async validateModel(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['model_id'], 'model.validate params')
    const modelId = requiredString(params.model_id, 'model_id')
    const model = (await this.dependencies.listModels()).find(item => item.id === modelId)
    if (!model)
      return { errors: ['model not found'] }
    return { errors: model.renderer === 'unsupported' ? ['unsupported model format'] : [] }
  }

  private async loadModel(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['model_id'], 'model.load params')
    const modelId = requiredString(params.model_id, 'model_id')
    const model = (await this.dependencies.listModels()).find(item => item.id === modelId)
    if (!model || model.renderer === 'unsupported')
      return { loaded: false }

    const generation = ++this.modelLoadGeneration
    this.resolveModelWaiters()
    if (this.state.stageMounted
      && this.state.modelLoaded
      && this.state.modelId === model.id
      && this.state.renderer === model.renderer) {
      this.activeModelLoad = undefined
      this.pendingModelLoad = undefined
      return { loaded: true }
    }

    this.activeModelLoad = { modelId: model.id, renderer: model.renderer }
    this.pendingModelLoad = { generation, modelId: model.id, renderer: model.renderer }
    this.state.modelId = model.id
    this.state.renderer = model.renderer
    this.state.modelLoaded = false
    try {
      await this.dependencies.selectModel(model.id)
      if (generation !== this.modelLoadGeneration)
        return { loaded: false }
      if (!this.state.modelLoaded)
        await this.waitForModelLoad(this.dependencies.modelLoadTimeoutMs ?? 3_000)
    }
    catch (error) {
      if (this.pendingModelLoad?.generation === generation)
        this.pendingModelLoad = undefined
      throw error
    }
    return {
      loaded: generation === this.modelLoadGeneration
        && this.state.modelId === model.id
        && this.state.modelLoaded,
    }
  }

  private async updateState(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['state'], 'state.update params')
    this.requireRendererReady()
    const state = parseAvatarState(params.state)
    await this.dependencies.applyState(state)
    this.state.stateSequence += 1
    this.state.expressionId = state.expression.expression_id
    this.state.valence = state.valence
    this.state.arousal = state.arousal
    return {}
  }

  private async triggerExpression(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['expression_id', 'intensity', 'duration_ms'], 'expression.trigger params')
    this.requireRendererReady()
    const expressionId = requiredString(params.expression_id, 'expression_id')
    const intensity = unitNumber(params.intensity, 'intensity')
    const durationMs = integerInRange(params.duration_ms, 'duration_ms', 0, 60_000)
    await this.dependencies.applyExpression(expressionId, intensity, durationMs)
    this.state.expressionId = expressionId
    this.state.expressionSequence += 1
    return {}
  }

  private async triggerGesture(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['gesture_id', 'intensity'], 'gesture.trigger params')
    this.requireRendererReady()
    const gestureId = requiredString(params.gesture_id, 'gesture_id')
    const intensity = unitNumber(params.intensity, 'intensity')
    await this.dependencies.applyGesture(gestureId, intensity)
    this.state.lastGestureId = gestureId
    this.state.gestureSequence += 1
    return {}
  }

  private async setProactiveLevel(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    exactKeys(params, ['level'], 'proactive.set_level params')
    this.requireRendererReady()
    const level = integerInRange(params.level, 'level', 0, 4)
    await this.dependencies.applyProactiveLevel(level)
    this.state.proactiveLevel = level
    this.state.proactiveSequence += 1
    return {}
  }

  private inspectStage(): Record<string, unknown> {
    return {
      renderer: this.state.renderer,
      model_id: this.state.modelId,
      model_loaded: this.state.modelLoaded,
      visible: this.state.stageMounted && this.state.modelLoaded && this.dependencies.isWindowVisible(),
      state_sequence: this.state.stateSequence,
      rendered_state_sequence: this.state.renderedStateSequence,
      frame_sequence: this.state.frameSequence,
      expression_sequence: this.state.expressionSequence,
      gesture_sequence: this.state.gestureSequence,
      proactive_sequence: this.state.proactiveSequence,
      expression_id: this.state.expressionId,
      valence: this.state.valence,
      arousal: this.state.arousal,
      proactive_level: this.state.proactiveLevel,
      last_gesture_id: this.state.lastGestureId,
    }
  }

  private async waitForModelLoad(timeoutMs: number): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      const done = () => {
        clearTimeout(timeout)
        this.modelWaiters.delete(done)
        resolve()
      }
      const timeout = setTimeout(() => {
        this.modelWaiters.delete(done)
        reject(new Error('renderer model load timed out'))
      }, timeoutMs)
      this.modelWaiters.add(done)
    })
  }

  private resolveModelWaiters(): void {
    for (const resolve of this.modelWaiters)
      resolve()
    this.modelWaiters.clear()
  }

  private requireRendererReady(): void {
    if (!this.state.stageMounted || !this.state.modelLoaded)
      throw new Error('renderer is not ready')
  }
}

function parseAvatarState(value: unknown): AvatarStateSnapshot {
  const state = record(value, 'state')
  const expression = record(state.expression, 'state.expression')
  const pose = record(state.pose, 'state.pose')
  const eyes = record(state.eyes, 'state.eyes')
  exactKeys(state, [
    'expression', 'pose', 'eyes', 'valence', 'arousal', 'energy', 'is_speaking', 'audio_level',
  ], 'state')
  exactKeys(expression, [
    'expression_id', 'intensity', 'mouth_open', 'eye_open', 'brow_raise', 'cheek_raise',
  ], 'state.expression')
  exactKeys(pose, [
    'pose_id', 'gesture_id', 'gesture_intensity', 'breathing_amplitude',
  ], 'state.pose')
  exactKeys(eyes, [
    'gaze_target', 'blink_rate', 'eye_contact_duration', 'pupil_dilation',
  ], 'state.eyes')
  return {
    expression: {
      expression_id: requiredString(expression.expression_id, 'expression_id'),
      intensity: unitNumber(expression.intensity, 'expression.intensity'),
      mouth_open: unitNumber(expression.mouth_open, 'expression.mouth_open'),
      eye_open: unitNumber(expression.eye_open, 'expression.eye_open'),
      brow_raise: unitNumber(expression.brow_raise, 'expression.brow_raise'),
      cheek_raise: unitNumber(expression.cheek_raise, 'expression.cheek_raise'),
    },
    pose: {
      pose_id: requiredString(pose.pose_id, 'pose.pose_id'),
      gesture_id: nullableString(pose.gesture_id, 'pose.gesture_id'),
      gesture_intensity: unitNumber(pose.gesture_intensity, 'pose.gesture_intensity'),
      breathing_amplitude: unitNumber(pose.breathing_amplitude, 'pose.breathing_amplitude'),
    },
    eyes: {
      gaze_target: requiredString(eyes.gaze_target, 'eyes.gaze_target'),
      blink_rate: nonnegativeNumber(eyes.blink_rate, 'eyes.blink_rate'),
      eye_contact_duration: nonnegativeNumber(eyes.eye_contact_duration, 'eyes.eye_contact_duration'),
      pupil_dilation: unitNumber(eyes.pupil_dilation, 'eyes.pupil_dilation'),
    },
    valence: numberInRange(state.valence, 'valence', -1, 1),
    arousal: unitNumber(state.arousal, 'arousal'),
    energy: unitNumber(state.energy, 'energy'),
    is_speaking: requiredBoolean(state.is_speaking, 'is_speaking'),
    audio_level: unitNumber(state.audio_level, 'audio_level'),
  }
}

function exactKeys(value: Record<string, unknown>, allowed: string[], name: string): void {
  const allowedKeys = new Set(allowed)
  const unknownKey = Object.keys(value).find(key => !allowedKeys.has(key))
  if (unknownKey)
    throw new Error(`${name} contains an unknown field`)
}

function record(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value))
    throw new Error(`${name} must be an object`)
  return value as Record<string, unknown>
}

function requiredString(value: unknown, name: string): string {
  if (typeof value !== 'string' || !value || value.length > 128)
    throw new Error(`${name} is invalid`)
  return value
}

function nullableString(value: unknown, name: string): string | null {
  if (value === null)
    return null
  return requiredString(value, name)
}

function requiredBoolean(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean')
    throw new Error(`${name} is invalid`)
  return value
}

function nonnegativeNumber(value: unknown, name: string): number {
  return numberInRange(value, name, 0, Number.MAX_VALUE)
}

function unitNumber(value: unknown, name: string): number {
  return numberInRange(value, name, 0, 1)
}

function numberInRange(value: unknown, name: string, minimum: number, maximum: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < minimum || value > maximum)
    throw new Error(`${name} is invalid`)
  return value
}

function integerInRange(value: unknown, name: string, minimum: number, maximum: number): number {
  const parsed = numberInRange(value, name, minimum, maximum)
  if (!Number.isSafeInteger(parsed))
    throw new Error(`${name} is invalid`)
  return parsed
}
