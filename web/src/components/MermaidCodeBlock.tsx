import CodeBlock from '@tiptap/extension-code-block'
import {
  NodeViewContent,
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from '@tiptap/react'
import { useState } from 'react'
import { Mermaid } from './Mermaid'

// Node view for code blocks in the WYSIWYG editor. For a ```mermaid block
// the default view is the live diagram alone — matching the read-side
// (Markdown.tsx), which renders only the graph — and the editable source
// stays collapsed behind a toggle the author opens to edit it. A brand
// new (empty) block opens with the source showing so there is somewhere
// to type; everything authored already lands graph-first. Every other
// language renders exactly like the default code block (an editable
// <pre><code>), so swapping the StarterKit code block for this one only
// adds the mermaid affordance and changes nothing else.
//
// The block stays a real ```fence in the stored markdown — the node name,
// attributes, input rules and tiptap-markdown round-trip are inherited
// unchanged from CodeBlock — so the same source renders read-side via the
// react-markdown ```mermaid handler (Markdown.tsx) and survives copy to
// any other markdown tool (GitHub, etc.).
// The file's purpose is the TipTap Node extension exported below (not a
// React component), which react-refresh flags on this node-view component.
// eslint-disable-next-line react-refresh/only-export-components
function CodeBlockView({ node }: NodeViewProps) {
  const language =
    typeof node.attrs.language === 'string' ? node.attrs.language : ''
  const isMermaid = language === 'mermaid'
  // Source is collapsed by default for a mermaid block (graph-only, like
  // the viewer); a fresh empty block opens expanded so there is somewhere
  // to type. Non-mermaid code blocks always show their source.
  const [showSource, setShowSource] = useState(
    () => !isMermaid || node.textContent.trim() === '',
  )
  // The source <pre> always stays mounted (CSS-hidden when collapsed) so
  // ProseMirror keeps its contentDOM and editing never breaks.
  return (
    <NodeViewWrapper
      className={
        'rte-cb' +
        (isMermaid ? ' rte-cb--mermaid' : '') +
        (isMermaid && !showSource ? ' rte-cb--rendered' : '')
      }
    >
      {isMermaid && (
        <div className="rte-cb__toolbar" contentEditable={false}>
          <button
            type="button"
            className="rte-cb__toggle"
            // preventDefault on mousedown keeps the editor selection put
            // instead of collapsing it into the chrome on click.
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => setShowSource((s) => !s)}
            aria-pressed={showSource}
          >
            {showSource ? 'Hide source' : 'Edit source'}
          </button>
        </div>
      )}
      <pre>
        {/* Explicit generic: NodeViewContent's ``as`` is NoInfer<T>, so the
            tag is not inferred from the prop and defaults to 'div' unless
            we pass it (@tiptap/react v3 type contract). */}
        <NodeViewContent<'code'>
          as="code"
          className={language ? `language-${language}` : undefined}
        />
      </pre>
      {isMermaid && (
        <div className="rte-cb__preview" contentEditable={false}>
          {/* node.textContent is the live source; the React node view
              re-renders as the user types, and Mermaid debounces. */}
          <Mermaid code={node.textContent} />
        </div>
      )}
    </NodeViewWrapper>
  )
}

export const CodeBlockMermaid = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(CodeBlockView)
  },
})
