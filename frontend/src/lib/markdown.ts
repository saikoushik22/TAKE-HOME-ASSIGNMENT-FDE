import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: true })

/**
 * Render Markdown to HTML for display inside the app shell.
 *
 * The server already sanitizes artifacts on write, so this is the second
 * layer rather than the only one. It runs anyway because this same function
 * renders assistant *messages*, which are raw model output that never passed
 * through the artifact sanitizer — and because a client that trusts the server
 * completely breaks the moment someone adds a new content path.
 *
 * Note this is for content rendered in the APP's origin, so it is deliberately
 * stricter than the artifact viewer: no <style>, no inline styles, no scripts.
 * Rich HTML artifacts get their capabilities from the sandboxed iframe instead,
 * where they cannot reach the app.
 */
export function renderMarkdown(source: string): string {
  const html = marked.parse(source ?? '', { async: false }) as string
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'span', 'sup', 'sub', 'img',
    ],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class', 'src', 'alt'],
    ALLOW_DATA_ATTR: false,
    // Only http(s), mailto, and data: images survive.
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|data:image\/(?:png|jpeg|gif|webp);base64,)/i,
  })
}

/** Force external links to open safely, without leaking the referrer. */
export function hardenLinks(root: HTMLElement | null): void {
  if (!root) return
  root.querySelectorAll('a[href]').forEach((anchor) => {
    anchor.setAttribute('target', '_blank')
    anchor.setAttribute('rel', 'noopener noreferrer nofollow')
  })
}

/** Rough word count used for the Ship 30 essay length indicator. */
export function wordCount(text: string): number {
  return (text.trim().match(/\S+/g) ?? []).length
}
