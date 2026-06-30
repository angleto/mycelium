import { test, expect } from '@playwright/test'
import { req, E2E_TAG_ID } from './_api'

// Verify task d37aacd7: markdown editor image upload via file picker.
// Drag/drop & paste paths are exercised by the same doUpload(); the
// picker path is the safest to drive in Playwright (no clipboard
// permissions, no synthetic DataTransfer).

interface TaskOut {
  id: string
  description: string | null
  version: number
}

// 67-byte minimal valid PNG (1x1 transparent).
const TINY_PNG = Buffer.from(
  '89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489000000' +
    '0A49444154789C636000000000020001E221BC330000000049454E44AE426082',
  'hex',
)

test.describe('task d37aacd7 — markdown image embed', () => {
  let taskId = ''

  test.afterAll(async () => {
    if (!taskId) return
    const cur = await req<{ version: number }>(`/tasks/${taskId}`)
    if (cur.data?.version) {
      await req(`/tasks/${taskId}/delete`, {
        method: 'POST',
        body: { expected_version: cur.data.version },
      })
    }
  })

  test('file picker uploads image and inserts markdown ![](attachment)', async ({
    page,
  }) => {
    const created = await req<TaskOut>('/tasks', {
      method: 'POST',
      body: {
        title: `e2e-claude task d37aacd7 ${Date.now()}`,
        description: 'before image',
        importance: 3,
        urgency: 3,
        necessity: 'should',
        tag_ids: [E2E_TAG_ID],
      },
    })
    taskId = created.data?.id ?? ''
    expect(created.status, JSON.stringify(created.data)).toBeLessThan(300)

    await page.goto(`/tasks/${taskId}`)
    // RichEditor lives under the description tabpanel of the task.
    const descPanel = page.locator('[role="tabpanel"]').filter({
      has: page.locator('.rte'),
    }).first()
    await expect(descPanel.locator('.rte')).toBeVisible({ timeout: 15_000 })

    // Switch to raw markdown so we can assert the inserted text directly
    // (WYSIWYG mode inserts a tiptap image node we'd have to inspect via
    // ProseMirror state; raw mode is the more robust check).
    await descPanel.getByRole('button', { name: /raw|markdown/i }).click()

    // Hidden file input — Playwright bypasses the click and feeds files
    // directly via setInputFiles.
    const fileInput = descPanel.locator('input[type="file"]')
    await fileInput.setInputFiles({
      name: 'e2e-claude-pixel.png',
      mimeType: 'image/png',
      buffer: TINY_PNG,
    })

    // Wait for the markdown image syntax to appear in the textarea.
    const textarea = descPanel.locator('textarea.rte__raw')
    await expect(textarea).toHaveValue(
      /!\[e2e-claude-pixel\.png\]\(\/attachments\/[^)]+\/download\)/,
      { timeout: 15_000 },
    )

    // Persist the change and verify via API that the description carries
    // the markdown image reference.
    const saveBtn = page.getByRole('button', { name: /^Save|Salva$/i })
    await saveBtn.first().click()
    await expect
      .poll(
        async () => {
          const r = await req<TaskOut>(`/tasks/${taskId}`)
          return r.data?.description ?? ''
        },
        { timeout: 15_000 },
      )
      .toMatch(/!\[e2e-claude-pixel\.png\]\(\/attachments\/[^)]+\/download\)/)
  })
})
