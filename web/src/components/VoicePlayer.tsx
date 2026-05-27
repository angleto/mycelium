import { useTranslation } from 'react-i18next'
import { useAuthBlobUrl } from '../lib/useAuthBlobUrl'

// Audio player for an existing voice note. The note's ``audio_ref``
// has the shape ``attachment:<uuid>``; we route it through
// useAuthBlobUrl so the bearer-auth /attachments/<id>/download path
// resolves to a one-shot object URL the <audio> tag can consume.

function srcFromAudioRef(audioRef: string | null | undefined): string | null {
  if (!audioRef) return null
  if (audioRef.startsWith('attachment:')) {
    const id = audioRef.slice('attachment:'.length)
    if (id) return `/attachments/${id}/download`
  }
  return null
}

export function VoicePlayer({
  audioRef,
  audioSeconds,
}: {
  audioRef: string | null | undefined
  audioSeconds?: number | null
}) {
  const { t } = useTranslation()
  const src = srcFromAudioRef(audioRef)
  const resolved = useAuthBlobUrl(src)
  if (!src) return null
  return (
    <div className="voice-player">
      <label className="voice-player__label">
        {t('notes.voicePlayer')}
        {audioSeconds && audioSeconds > 0 && (
          <span className="muted">
            {' '}
            ({Math.floor(audioSeconds / 60)}:
            {String(audioSeconds % 60).padStart(2, '0')})
          </span>
        )}
      </label>
      {resolved ? (
        <audio src={resolved} controls preload="metadata" />
      ) : (
        <span className="muted">{t('common.loading')}</span>
      )}
    </div>
  )
}
