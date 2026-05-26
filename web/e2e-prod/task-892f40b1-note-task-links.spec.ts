import { test, expect } from '@playwright/test'
import { req, E2E_TAG_ID } from './_api'

// Verify task 892f40b1: a note linked to multiple tasks must show all
// of them in the panel. The user observed "nota enciclica con due
// task ma ne vedo solo uno" — this test reproduces with 2 derived
// tasks and confirms both render.

interface NoteOut {
  id: string
  version: number
  title: string
}
interface TaskOut {
  id: string
  version: number
}

test.describe('task 892f40b1 — note↔task links visibility', () => {
  let noteId = ''
  const taskIds: string[] = []

  test.afterAll(async () => {
    for (const tid of taskIds) {
      const cur = await req<{ version: number }>(`/tasks/${tid}`)
      if (cur.data?.version) {
        await req(`/tasks/${tid}/delete`, {
          method: 'POST',
          body: { expected_version: cur.data.version },
        })
      }
    }
    if (noteId) {
      const cur = await req<{ version: number }>(`/notes/${noteId}`)
      if (cur.data?.version) {
        await req(`/notes/${noteId}/delete`, {
          method: 'POST',
          body: { expected_version: cur.data.version },
        })
      }
    }
  })

  test('panel shows all linked tasks (derive 2 + subject 1)', async ({
    page,
  }) => {
    // Tall viewport so the modal's body shows the LinkedTasksPanel
    // without forcing the user to scroll past the editor.
    await page.setViewportSize({ width: 1280, height: 1600 })
    // 1) Note with placeholder transcript so derive-task has something
    //    to work with.
    const stamp = Date.now()
    const noteRes = await req<NoteOut>('/notes', {
      method: 'POST',
      body: {
        title: `e2e-claude note 892f40b1 ${stamp}`,
        transcript: 'first todo line\nsecond todo line',
        kind: 'text',
        tag_ids: [E2E_TAG_ID],
      },
    })
    expect(noteRes.status, JSON.stringify(noteRes.data)).toBeLessThan(300)
    noteId = noteRes.data!.id

    // 2) Two derive-task calls. The endpoint creates a task and a
    //    derived_from link from the note in a single shot.
    const d1 = await req<{ task_id: string }>(`/notes/${noteId}/derive-task`, {
      method: 'POST',
      body: { title: `e2e-claude derived A ${stamp}` },
      retry404: true,
    })
    expect(d1.status, JSON.stringify(d1.data)).toBeLessThan(300)
    taskIds.push(d1.data!.task_id)

    const d2 = await req<{ task_id: string }>(`/notes/${noteId}/derive-task`, {
      method: 'POST',
      body: { title: `e2e-claude derived B ${stamp}` },
      retry404: true,
    })
    expect(d2.status, JSON.stringify(d2.data)).toBeLessThan(300)
    taskIds.push(d2.data!.task_id)

    // 3) Third task plus a 'subject' link, to also cover the picker
    //    side of the panel.
    const t3 = await req<TaskOut>('/tasks', {
      method: 'POST',
      body: {
        title: `e2e-claude subject C ${stamp}`,
        importance: 3,
        urgency: 3,
        necessity: 'should',
        tag_ids: [E2E_TAG_ID],
      },
    })
    expect(t3.status, JSON.stringify(t3.data)).toBeLessThan(300)
    taskIds.push(t3.data!.id)
    const ln = await req(`/notes/${noteId}/task-links`, {
      method: 'POST',
      body: { task_id: t3.data!.id, kind: 'subject' },
      retry404: true,
    })
    expect(ln.status).toBeLessThan(300)

    // 4) API self-check: GET /notes/{id}/links must report all 3 links
    //    (independent of the SPA). If this fails the bug is server-side.
    interface LinksOut {
      task_links: { task_id: string; kind: string }[]
    }
    const links = await req<LinksOut>(`/notes/${noteId}/links`, {
      retry404: true,
    })
    expect(links.status).toBe(200)
    const linkedIds = new Set(links.data?.task_links.map((l) => l.task_id))
    for (const tid of taskIds) {
      expect(linkedIds.has(tid), `API: link missing for task ${tid}`).toBe(true)
    }

    // 5) Open the note in the SPA. NotesRoute only renders the
    //    LinkedTasksPanel inside the note edit drawer.
    await page.goto('/notes')
    await page.waitForSelector('.noteitem', { timeout: 15_000 })
    await page
      .locator('.noteitem')
      .filter({ hasText: `e2e-claude note 892f40b1 ${stamp}` })
      .locator('.noteitem__title')
      .click()
    const panel = page.locator('.linkedpanel')
    await expect(panel).toBeVisible({ timeout: 15_000 })
    await panel.scrollIntoViewIfNeeded()

    // Assert all 3 linked-task titles render in the panel.
    for (const tid of taskIds) {
      const link = panel.locator(`a[href$="/tasks/${tid}"]`)
      await expect(link, `SPA: missing link to task ${tid}`).toBeVisible({
        timeout: 15_000,
      })
    }
  })
})
