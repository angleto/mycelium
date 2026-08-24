import { useEffect, useId, useState } from 'react'
import { useEffectiveTheme } from '../lib/useIsDark'

// Mermaid diagram rendering, shared by the read-side markdown renderer
// (the ```mermaid fenced block in Markdown.tsx) and the write-side editor
// (the MermaidWidget block preview in lib/markdownSource/widgets.ts).
//
// Mermaid is a heavy dependency (hundreds of KB with its d3 / cytoscape
// stack), and a diagram is only present in a minority of notes, so the
// library is code-split behind a dynamic import and pulled in on first
// use. The module-level promise makes that load a singleton shared by
// every diagram on the page.
type MermaidApi = (typeof import('mermaid'))['default']
let mermaidPromise: Promise<MermaidApi> | null = null
function loadMermaid(): Promise<MermaidApi> {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((m) => m.default)
  }
  return mermaidPromise
}

// Effective light/dark detection (reacting to the forced theme and, on
// 'auto', the OS preference) lives in lib/useIsDark so the Time donut and
// any other per-theme palette share one implementation. Diagrams
// re-render when it flips so a graph never stays light-on-dark.

// `securityLevel: 'strict'` is mermaid's safe default: it runs the output
// through DOMPurify, disables HTML labels and click handlers, so the SVG
// we inject is sanitised. That matters because note bodies are authored
// content shared across a workspace (comments / suggestions), so a
// diagram must not become an XSS vector.
export function Mermaid({ code }: { code: string }) {
  const theme = useEffectiveTheme()
  const [svg, setSvg] = useState('')
  const [error, setError] = useState<string | null>(null)
  // A DOM-id-safe unique key for mermaid.render (React's useId carries
  // colons, which are invalid in the element id mermaid derives from it).
  const id = 'mmd-' + useId().replace(/[^a-zA-Z0-9-]/g, '')

  useEffect(() => {
    let cancelled = false
    const src = code.trim()
    // Debounce so live-typing in the editor preview doesn't render on
    // every keystroke; a static read-side diagram just waits this once.
    // The empty-source reset lives inside the timer too, so the effect
    // body never calls setState synchronously (react-hooks).
    const timer = window.setTimeout(() => {
      void (async () => {
        if (!src) {
          if (cancelled) return
          setSvg('')
          setError(null)
          return
        }
        try {
          const mermaid = await loadMermaid()
          if (cancelled) return
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: theme === 'dark' ? 'dark' : 'default',
          })
          // Validate first: a half-typed diagram makes mermaid.render
          // throw and can leave orphan DOM behind. `suppressErrors`
          // returns false instead of throwing.
          const parsed = await mermaid.parse(src, { suppressErrors: true })
          if (cancelled) return
          if (!parsed) {
            setError('Invalid diagram syntax')
            return
          }
          const res = await mermaid.render(id, src)
          if (cancelled) return
          setSvg(res.svg)
          setError(null)
        } catch (e) {
          if (cancelled) return
          setError(e instanceof Error ? e.message : String(e))
        }
      })()
    }, 200)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [code, theme, id])

  if (error) {
    return (
      <div className="mermaid-block mermaid-block--error" role="img">
        <pre className="mermaid-block__source" title={error}>
          {code.trim()}
        </pre>
        <span className="mermaid-block__hint">⚠ {error}</span>
      </div>
    )
  }
  return (
    <div
      className="mermaid-block"
      role="img"
      aria-label="diagram"
      // Sanitised by mermaid's strict securityLevel (DOMPurify) above.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}
