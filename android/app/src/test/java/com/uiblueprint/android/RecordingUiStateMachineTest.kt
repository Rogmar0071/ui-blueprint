package com.uiblueprint.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordingUiStateMachineTest {
    @Test
    fun `capture error resets state to idle`() {
        val stateMachine = RecordingUiStateMachine()
        stateMachine.onRecordingStarted()

        val transition = stateMachine.onCaptureCompleted(CaptureDoneEvent(error = "Capture failed"))

        assertEquals(RecordingUiStatus.IDLE, transition.state.state)
        assertEquals("Capture failed", transition.state.lastError)
        assertEquals(
            RecordingUiEffect.ShowError("Capture failed"),
            transition.effect,
        )
    }

    @Test
    fun `capture success resets state to idle and enqueues upload`() {
        val stateMachine = RecordingUiStateMachine()
        stateMachine.onRecordingStarted()

        val transition = stateMachine.onCaptureCompleted(
            CaptureDoneEvent(clipPath = "/tmp/capture_20260408.mp4"),
        )

        assertEquals(RecordingUiStatus.IDLE, transition.state.state)
        assertEquals("capture_20260408.mp4", transition.state.lastClipLabel)
        assertEquals(
            RecordingUiEffect.EnqueueUpload(
                clipPath = "/tmp/capture_20260408.mp4",
                clipLabel = "capture_20260408.mp4",
            ),
            transition.effect,
        )
    }

    @Test
    fun `missing clip and error resets state to idle with fallback error`() {
        val stateMachine = RecordingUiStateMachine()
        stateMachine.onRecordingStarted()

        val transition = stateMachine.onCaptureCompleted(CaptureDoneEvent())

        assertEquals(RecordingUiStatus.IDLE, transition.state.state)
        assertEquals(CaptureDoneEvent.ERROR_NO_OUTPUT, transition.state.lastError)
        assertEquals(
            RecordingUiEffect.ShowError(CaptureDoneEvent.ERROR_NO_OUTPUT),
            transition.effect,
        )
    }

    @Test
    fun `watchdog timeout resets state to idle`() {
        val stateMachine = RecordingUiStateMachine()
        stateMachine.onRecordingStarted()

        val transition = stateMachine.onWatchdogTimeout()

        assertEquals(RecordingUiStatus.IDLE, transition.state.state)
        assertEquals(CaptureDoneEvent.ERROR_TIMEOUT, transition.state.lastError)
        assertTrue(transition.effect is RecordingUiEffect.ShowError)
    }
}
