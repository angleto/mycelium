import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

// Browser MediaRecorder wrapper. Asks for mic permission on first
// ``Record`` click, captures WebM/Opus (Chrome default; Safari may
// return audio/mp4), surfaces the resulting Blob + duration to the
// parent. The parent uploads + sets note.audio_ref + triggers
// transcribe — this component stays mute / dumb on persistence.
//
// State machine: idle -> recording -> recorded -> (re-record -> idle).
export function VoiceRecorder({
  onRecorded,
}: {
  onRecorded: (blob: Blob, mimeType: string, durationSec: number) => void
}) {
  const { t } = useTranslation()
  const [phase, setPhase] = useState<'idle' | 'recording' | 'recorded'>('idle')
  const [elapsed, setElapsed] = useState(0)
  const [err, setErr] = useState<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const startedAt = useRef<number>(0)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current != null) window.clearInterval(timerRef.current)
      if (streamRef.current) {
        for (const tr of streamRef.current.getTracks()) tr.stop()
      }
      if (previewUrl) URL.revokeObjectURL(previewUrl)
    }
  }, [previewUrl])

  async function start() {
    setErr(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      // Pick a mime the browser supports. Chrome/Firefox prefer
      // ``audio/webm`` (Opus); Safari returns ``audio/mp4``. Whatever
      // the recorder produces is whatever we upload.
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : ''
      const mr = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream)
      recorderRef.current = mr
      chunksRef.current = []
      mr.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
      }
      mr.onstop = () => {
        const blob = new Blob(chunksRef.current, {
          type: mr.mimeType || 'audio/webm',
        })
        const dur = Math.max(1, Math.round((Date.now() - startedAt.current) / 1000))
        const url = URL.createObjectURL(blob)
        setPreviewUrl(url)
        setPhase('recorded')
        if (streamRef.current) {
          for (const tr of streamRef.current.getTracks()) tr.stop()
          streamRef.current = null
        }
        onRecorded(blob, blob.type, dur)
      }
      mr.start()
      startedAt.current = Date.now()
      setElapsed(0)
      setPhase('recording')
      timerRef.current = window.setInterval(() => {
        setElapsed(Math.round((Date.now() - startedAt.current) / 1000))
      }, 250)
    } catch (e) {
      setErr((e as Error).message)
      setPhase('idle')
    }
  }

  function stop() {
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop()
    }
    if (timerRef.current != null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  function reset() {
    if (previewUrl) URL.revokeObjectURL(previewUrl)
    setPreviewUrl(null)
    chunksRef.current = []
    setPhase('idle')
    setElapsed(0)
  }

  return (
    <div className="voicerec">
      {err && <p className="err">{err}</p>}
      {phase === 'idle' && (
        <button
          type="button"
          className="btn voicerec__rec"
          onClick={() => void start()}
        >
          ● {t('notes.voiceRecord')}
        </button>
      )}
      {phase === 'recording' && (
        <div className="row">
          <button type="button" className="btn voicerec__stop" onClick={stop}>
            ■ {t('notes.voiceStop')}
          </button>
          <span className="muted voicerec__pulse">
            {String(Math.floor(elapsed / 60)).padStart(2, '0')}:
            {String(elapsed % 60).padStart(2, '0')}
          </span>
        </div>
      )}
      {phase === 'recorded' && previewUrl && (
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <audio src={previewUrl} controls />
          <button type="button" className="btn--ghost btn--sm" onClick={reset}>
            {t('notes.voiceReRecord')}
          </button>
        </div>
      )}
    </div>
  )
}
