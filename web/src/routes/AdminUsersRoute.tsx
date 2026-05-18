import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { setAdminMode } from '../auth/session'
import { useAdminMode } from '../auth/useSession'
import { useMe } from '../auth/useMe'
import type { components } from '../api/schema'

type AdminUser = components['schemas']['AdminUserOut']

// User administration. Only reachable while elevated; the server
// re-checks the capability + X-Admin-Mode on every call, this guard is
// only for UX (a normal-mode admin sees a prompt, not a raw 403).
export function AdminUsersRoute() {
  const { t } = useTranslation()
  const { me, loading: meLoading } = useMe()
  const elevated = useAdminMode()
  const [rows, setRows] = useState<AdminUser[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)

  // Used by the post-mutation refresh (event handlers may setState
  // synchronously; the effect must not — hence the inline IIFE below).
  const load = useCallback(async () => {
    const { data, error } = await api.GET('/admin/users')
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setErr(null)
    setRows(data)
    setLoaded(true)
  }, [])

  useEffect(() => {
    if (!me?.is_admin || !elevated) return
    let active = true
    void (async () => {
      const { data, error } = await api.GET('/admin/users')
      if (!active) return
      if (error || !data) {
        setErr(errMessage(error))
        return
      }
      setErr(null)
      setRows(data)
      setLoaded(true)
    })()
    return () => {
      active = false
    }
  }, [me?.is_admin, elevated])

  async function patch(u: AdminUser, body: Partial<AdminUser>) {
    setErr(null)
    const { error } = await api.PATCH('/admin/users/{user_id}', {
      params: { path: { user_id: u.id } },
      body,
    })
    if (error) {
      setErr(errMessage(error))
      return
    }
    await load()
  }

  if (meLoading) return <p>{t('admin.loading')}</p>
  if (!me?.is_admin) return <p className="err">{t('admin.forbidden')}</p>

  return (
    <>
      <h1 className="page-title">{t('admin.usersTitle')}</h1>
      {!elevated ? (
        <section className="card">
          <p className="hint">{t('admin.notElevated')}</p>
          <button type="button" onClick={() => setAdminMode(true)}>
            {t('admin.enter')}
          </button>
        </section>
      ) : (
        <section className="card">
          {err && <p className="err">{err}</p>}
          {!loaded && !err ? (
            <p>{t('admin.loading')}</p>
          ) : (
            <table className="tbl">
              <thead>
                <tr>
                  <th>{t('admin.colEmail')}</th>
                  <th>{t('admin.colRole')}</th>
                  <th>{t('admin.colStatus')}</th>
                  <th>{t('admin.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((u) => {
                  const self = u.id === me.user_id
                  return (
                    <tr key={u.id}>
                      <td>
                        {u.email}
                        {self && (
                          <span className="muted"> ({t('admin.you')})</span>
                        )}
                      </td>
                      <td>
                        {u.is_admin ? (
                          <strong>{t('admin.roleAdmin')}</strong>
                        ) : (
                          <span className="muted">{t('admin.roleUser')}</span>
                        )}
                      </td>
                      <td>
                        {u.is_active ? (
                          t('admin.active')
                        ) : (
                          <span className="err">{t('admin.blocked')}</span>
                        )}
                      </td>
                      <td className="adminacts">
                        <button
                          type="button"
                          className="btn--sm btn--ghost"
                          disabled={self}
                          title={self ? t('admin.selfLock') : undefined}
                          onClick={() =>
                            void patch(u, { is_admin: !u.is_admin })
                          }
                        >
                          {u.is_admin
                            ? t('admin.revokeAdmin')
                            : t('admin.makeAdmin')}
                        </button>
                        <button
                          type="button"
                          className={
                            u.is_active
                              ? 'btn--sm btn--danger'
                              : 'btn--sm btn--ghost'
                          }
                          disabled={self}
                          title={self ? t('admin.selfLock') : undefined}
                          onClick={() =>
                            void patch(u, { is_active: !u.is_active })
                          }
                        >
                          {u.is_active
                            ? t('admin.block')
                            : t('admin.unblock')}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </section>
      )}
    </>
  )
}
