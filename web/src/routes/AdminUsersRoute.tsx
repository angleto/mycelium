import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api, errMessage } from '../api/client'
import { setAdminMode } from '../auth/session'
import { useAdminMode } from '../auth/useSession'
import { useMe } from '../auth/useMe'
import type { components } from '../api/schema'

type AdminUser = components['schemas']['AdminUserOut']

// One server page. The endpoint used to return every user row; on a
// deployment with tens of thousands of accounts that alone stalled the
// page, so the list is now paged and, more importantly, searchable —
// nobody finds one person by walking a thousand pages.
const PAGE = 50

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
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    if (!me?.is_admin || !elevated) return
    let active = true
    // Debounced: the search box refetches per keystroke otherwise.
    const timer = setTimeout(() => {
      void (async () => {
        const { data, error } = await api.GET('/admin/users', {
          params: { query: { q: q || undefined, limit: PAGE, offset: 0 } },
        })
        if (!active) return
        if (error || !data) {
          setErr(errMessage(error))
          return
        }
        setErr(null)
        setRows(data)
        setOffset(data.length)
        // A full page means there is probably another one. It over-reports
        // when the total is an exact multiple of PAGE (one click, one empty
        // page); the alternative is a total count the endpoint does not
        // return, and this is what BillingRoute already does.
        setMore(data.length === PAGE)
        setLoaded(true)
      })()
    }, 250)
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [me?.is_admin, elevated, q])

  const loadMore = useCallback(async () => {
    if (loadingMore || !more) return
    setLoadingMore(true)
    const { data, error } = await api.GET('/admin/users', {
      params: { query: { q: q || undefined, limit: PAGE, offset } },
    })
    setLoadingMore(false)
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    setRows((prev) => [...prev, ...data])
    setOffset((prev) => prev + data.length)
    setMore(data.length === PAGE)
  }, [loadingMore, more, offset, q])

  async function patch(u: AdminUser, body: Partial<AdminUser>) {
    setErr(null)
    const { data, error } = await api.PATCH('/admin/users/{user_id}', {
      params: { path: { user_id: u.id } },
      body,
    })
    if (error || !data) {
      setErr(errMessage(error))
      return
    }
    // Splice the returned row in place rather than refetching: a reload
    // would drop the search term and every page loaded after the first.
    setRows((prev) => prev.map((r) => (r.id === data.id ? data : r)))
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
          {/* Stable class, not a placeholder match: the e2e locator must
              not depend on a translated string. */}
          <input
            className="adminsearch"
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={t('admin.search')}
            aria-label={t('admin.search')}
          />
          {!loaded && !err ? (
            <p>{t('admin.loading')}</p>
          ) : (
            <div
              className="scrollbox"
              onScroll={(e) => {
                const el = e.currentTarget
                if (
                  el.scrollHeight - el.scrollTop - el.clientHeight < 80 &&
                  more &&
                  !loadingMore
                ) {
                  void loadMore()
                }
              }}
            >
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
            </div>
          )}
          {loaded && rows.length === 0 && (
            <p className="hint">{t('admin.noMatch')}</p>
          )}
          {loaded && rows.length > 0 && !more && (
            <p className="hint">{t('admin.end')}</p>
          )}
        </section>
      )}
    </>
  )
}
