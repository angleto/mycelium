import CodeBlock from '@tiptap/extension-code-block'
import {
  NodeViewContent,
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from '@tiptap/react'
import { Mermaid } from './Mermaid'

// Node view for code blocks in the WYSIWYG editor. For a ```mermaid block
// it renders the editable source AND a live diagram preview beneath it,
// so the author can iterate on the source and see the graph update. Every
// other language renders exactly like the default code block (an editable
// <pre><code>), so swapping the StarterKit code block for this one only
// adds the mermaid affordance and changes nothing else.
//
// The block stays a real ```fence in the stored markdown — the node name,
// attributes, input rules and tiptap-markdown round-trip are inherited
// unchanged from CodeBlock — so the same source renders read-side via the
// react-markdown ```mermaid handler (Markdown.tsx) and survives copy to
// any other markdown tool (GitHub, etc.).
function CodeBlockView({ node }: NodeViewProps) {
  const language =
    typeof node.attrs.language === 'string' ? node.attrs.language : ''
  const isMermaid = language === 'mermaid'
  return (
    <NodeViewWrapper className={'rte-cb' + (isMermaid ? ' rte-cb--mermaid' : '')}>
      <pre>
        <NodeViewContent
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
