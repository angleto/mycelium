import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

type BuildInfo = {
  version: string
  git_sha: string
  git_sha_short: string
  built_at: string
}

function formatBuildAt(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString()
}

export function BuildInfo() {
  const { t } = useTranslation()
  const [info, setInfo] = useState<BuildInfo | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const res = await fetch('/api/buildinfo', { credentials: 'include' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = (await res.json()) as BuildInfo
        if (active) setInfo(data)
      } catch (e) {
        if (active) setErr(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      active = false
    }
  }, [])

  return (
    <section className="card">
      <h2>{t('buildinfo.title')}</h2>
      {err && <p className="err">{err}</p>}
      {!info && !err && <p>{t('home.loading')}</p>}
      {info && (
        <dl className="kv">
          <dt>{t('buildinfo.version')}</dt>
          <dd>
            <code>{info.version}</code>
          </dd>
          <dt>{t('buildinfo.commit')}</dt>
          <dd>
            {info.git_sha ? (
              <a
                href={`https://github.com/angleto/mycelium/commit/${info.git_sha}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                <code>{info.git_sha_short}</code>
              </a>
            ) : (
              <code>—</code>
            )}
          </dd>
          <dt>{t('buildinfo.builtAt')}</dt>
          <dd>{info.built_at ? formatBuildAt(info.built_at) : '—'}</dd>
        </dl>
      )}
    </section>
  )
}
