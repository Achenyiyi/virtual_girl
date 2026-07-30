"""Static release gates for the pinned AIRI integration patch."""

from __future__ import annotations

from pathlib import Path

PATCH_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "airi-v0.11.3"
    / "airi-v0.11.3-avatar-bridge.patch"
)


def test_airi_patch_contains_managed_updater_fail_closed_path() -> None:
    patch = PATCH_PATH.read_text(encoding="utf-8")

    required_fragments = (
        "disabled: Boolean(process.env.COMPANION_AVATAR_TOKEN?.trim())",
        "if (options.disabled)",
        "const state: AutoUpdaterState = { status: 'disabled' }",
        "async checkForUpdates() {}",
        "async downloadUpdate() {}",
        "async quitAndInstall() {}",
        "expect(fetchSpy).not.toHaveBeenCalled()",
        "Updates are disabled while AIRI is managed by Virtual Companion.",
    )
    for fragment in required_fragments:
        assert fragment in patch


def test_airi_patch_keeps_bridge_and_renderer_wiring() -> None:
    patch = PATCH_PATH.read_text(encoding="utf-8")

    required_paths = (
        "apps/stage-tamagotchi/src/main/services/airi/avatar-bridge/server.ts",
        "apps/stage-tamagotchi/src/main/services/airi/avatar-bridge/managed-avatar.ts",
        "apps/stage-tamagotchi/src/shared/avatar-bridge/renderer-runtime.ts",
        "packages/stage-ui-live2d/src/composables/live2d/avatar-semantics.ts",
    )
    for path in required_paths:
        assert f"diff --git a/{path} b/{path}" in patch
        assert "new file mode 100644" in patch.split(
            f"diff --git a/{path} b/{path}", maxsplit=1
        )[1].split("diff --git ", maxsplit=1)[0]


def test_airi_patch_pins_and_selects_the_managed_vrm() -> None:
    patch = PATCH_PATH.read_text(encoding="utf-8")

    required_fragments = (
        "COMPANION_AVATAR_MODEL_PATH",
        "COMPANION_AVATAR_MODEL_SHA256",
        "COMPANION_AVATAR_MODEL_ID",
        "managed avatar digest does not match",
        "managed avatar does not contain VRM metadata",
        "protocol.registerSchemesAsPrivileged",
        "registerManagedDisplayModel",
        "return displayModelsStore.getManagedDisplayModel()?.id ?? requestedId",
        "stageModelSelectedState.value = resolveStageModelId(value)",
        "const resolvedModelId = resolveStageModelId(selectedModelId)",
        "keeps a managed avatar selected over persisted and card model choices",
        "settingsStore.stageModelSelected = managedAvatar.id",
        "playAvatarGesture(gestureId: string, intensity = 1)",
        "modelRef.value.playAvatarGesture(gestureId, intensity)",
        "getNormalizedBoneNode('neck')",
        "updateAvatarGestureFrame(performance.now())",
        "interruptAvatarGesture('VRM gesture was interrupted because the model changed')",
    )
    for fragment in required_fragments:
        assert fragment in patch

    assert (
        "diff --git a/packages/stage-ui-three/src/components/Model/VRMModel.vue "
        "b/packages/stage-ui-three/src/components/Model/VRMModel.vue"
    ) in patch
    assert "modelRef.value?.vrm" not in patch
