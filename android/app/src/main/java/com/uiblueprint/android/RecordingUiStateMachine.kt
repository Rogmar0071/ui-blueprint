package com.uiblueprint.android

enum class RecordingUiStatus {
    IDLE,
    REQUESTING_PERMISSION,
    RECORDING,
}

data class UiRecordingState(
    val schemaVersion: String = CaptureDoneEvent.SCHEMA_VERSION,
    val state: RecordingUiStatus = RecordingUiStatus.IDLE,
    val lastError: String? = null,
    val lastClipLabel: String? = null,
)

sealed interface RecordingUiEffect {
    data object None : RecordingUiEffect
    data class ShowError(val message: String) : RecordingUiEffect
    data class EnqueueUpload(val clipPath: String, val clipLabel: String) : RecordingUiEffect
}

data class RecordingUiTransition(
    val state: UiRecordingState,
    val effect: RecordingUiEffect = RecordingUiEffect.None,
)

class RecordingUiStateMachine(
    initialState: UiRecordingState = UiRecordingState(),
) {
    var state: UiRecordingState = initialState
        private set

    fun onRecordRequested(): RecordingUiTransition = transition(
        UiRecordingState(
            state = RecordingUiStatus.REQUESTING_PERMISSION,
            lastClipLabel = state.lastClipLabel,
        ),
    )

    fun onRecordingStarted(): RecordingUiTransition = transition(
        UiRecordingState(
            state = RecordingUiStatus.RECORDING,
            lastClipLabel = state.lastClipLabel,
        ),
    )

    fun onPermissionDenied(message: String): RecordingUiTransition = transition(
        UiRecordingState(
            state = RecordingUiStatus.IDLE,
            lastError = message,
            lastClipLabel = state.lastClipLabel,
        ),
        RecordingUiEffect.ShowError(message),
    )

    fun onCaptureCompleted(event: CaptureDoneEvent): RecordingUiTransition {
        val normalizedEvent = event.normalized()
        val error = normalizedEvent.error
        if (error != null) {
            return transition(
                UiRecordingState(
                    state = RecordingUiStatus.IDLE,
                    lastError = error,
                    lastClipLabel = state.lastClipLabel,
                ),
                RecordingUiEffect.ShowError(error),
            )
        }

        val clipPath = normalizedEvent.clipPath ?: return onCaptureCompleted(
            CaptureDoneEvent(error = CaptureDoneEvent.ERROR_NO_OUTPUT),
        )
        val clipLabel = normalizedEvent.clipLabel().orEmpty()
        return transition(
            UiRecordingState(
                state = RecordingUiStatus.IDLE,
                lastClipLabel = clipLabel,
            ),
            RecordingUiEffect.EnqueueUpload(clipPath, clipLabel),
        )
    }

    fun onWatchdogTimeout(): RecordingUiTransition = transition(
        UiRecordingState(
            state = RecordingUiStatus.IDLE,
            lastError = CaptureDoneEvent.ERROR_TIMEOUT,
            lastClipLabel = state.lastClipLabel,
        ),
        RecordingUiEffect.ShowError(CaptureDoneEvent.ERROR_TIMEOUT),
    )

    fun onIdle(): RecordingUiTransition = transition(UiRecordingState(lastClipLabel = state.lastClipLabel))

    private fun transition(
        nextState: UiRecordingState,
        effect: RecordingUiEffect = RecordingUiEffect.None,
    ): RecordingUiTransition {
        state = nextState
        return RecordingUiTransition(state = nextState, effect = effect)
    }
}
