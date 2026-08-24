import { useEffect, useRef, useState } from 'react'
import { Icon } from './common'
import type { AppConfig, SessionSummary } from '../lib/types'

/** Groups sessions by recency so the list stays scannable as it grows. */
function groupByRecency(sessions: SessionSummary[]) {
  const now = Date.now()
  const day = 86_400_000
  const groups: Array<{ label: string; items: SessionSummary[] }> = [
    { label: 'Today', items: [] },
    { label: 'Previous 7 days', items: [] },
    { label: 'Earlier', items: [] },
  ]

  for (const session of sessions) {
    const age = now - new Date(session.updated_at).getTime()
    const bucket = age < day ? 0 : age < 7 * day ? 1 : 2
    groups[bucket]!.items.push(session)
  }

  return groups.filter((group) => group.items.length > 0)
}

export function Sidebar({
  sessions,
  activeId,
  config,
  open,
  theme,
  onNew,
  onSelect,
  onRename,
  onDelete,
  onClose,
  onToggleTheme,
}: {
  sessions: SessionSummary[]
  activeId: string | null
  config: AppConfig | null
  open: boolean
  theme: 'light' | 'dark'
  onNew: () => void
  onSelect: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
  onClose: () => void
  onToggleTheme: () => void
}) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editingId) inputRef.current?.select()
  }, [editingId])

  const commit = () => {
    if (editingId && draft.trim()) onRename(editingId, draft.trim())
    setEditingId(null)
  }

  const corpus = config?.corpus

  return (
    <nav className="sidebar" data-open={open} aria-label="Conversations">
      <div className="sidebar__head">
        <span className="sidebar__brand">Lenny Growth Assistant</span>
        <button
          type="button"
          className="btn btn--ghost btn--icon sidebar__toggle"
          onClick={onClose}
          aria-label="Close sidebar"
        >
          <Icon.Close />
        </button>
      </div>

      <div style={{ padding: '0 12px 8px' }}>
        <button
          type="button"
          className="btn btn--primary"
          style={{ width: '100%' }}
          onClick={onNew}
        >
          <Icon.Plus /> New chat
        </button>
      </div>

      <ul className="sidebar__list">
        {sessions.length === 0 && (
          <li style={{ padding: '10px 8px', fontSize: 13, color: 'var(--text-faint)' }}>
            No conversations yet.
          </li>
        )}

        {groupByRecency(sessions).map((group) => (
          <li key={group.label}>
            <p className="sidebar__group">{group.label}</p>
            <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {group.items.map((session) => (
                <li key={session.id}>
                  {editingId === session.id ? (
                    <input
                      ref={inputRef}
                      className="session"
                      style={{ background: 'var(--bg-raised)' }}
                      value={draft}
                      aria-label="Rename conversation"
                      onChange={(e) => setDraft(e.target.value)}
                      onBlur={commit}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commit()
                        if (e.key === 'Escape') setEditingId(null)
                      }}
                    />
                  ) : (
                    <div
                      className="session"
                      aria-current={session.id === activeId}
                      role="button"
                      tabIndex={0}
                      onClick={() => onSelect(session.id)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onSelect(session.id)
                        }
                      }}
                    >
                      <span className="session__title">{session.title}</span>

                      {confirmId === session.id ? (
                        <span className="session__actions" style={{ opacity: 1 }}>
                          <button
                            type="button"
                            className="btn btn--ghost btn--icon"
                            title="Confirm delete"
                            aria-label={`Confirm delete ${session.title}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              onDelete(session.id)
                              setConfirmId(null)
                            }}
                          >
                            <Icon.Check />
                          </button>
                          <button
                            type="button"
                            className="btn btn--ghost btn--icon"
                            title="Cancel"
                            aria-label="Cancel delete"
                            onClick={(e) => {
                              e.stopPropagation()
                              setConfirmId(null)
                            }}
                          >
                            <Icon.Close />
                          </button>
                        </span>
                      ) : (
                        <span className="session__actions">
                          <button
                            type="button"
                            className="btn btn--ghost btn--icon"
                            title="Rename"
                            aria-label={`Rename ${session.title}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              setDraft(session.title)
                              setEditingId(session.id)
                            }}
                          >
                            <Icon.Pencil />
                          </button>
                          <button
                            type="button"
                            className="btn btn--ghost btn--icon"
                            title="Delete"
                            aria-label={`Delete ${session.title}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              setConfirmId(session.id)
                            }}
                          >
                            <Icon.Trash />
                          </button>
                        </span>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>

      <div className="sidebar__foot">
        {corpus && (
          <span title="Knowledge base currently indexed">
            {corpus.ready
              ? `${corpus.episodes ?? 0} episodes · ${(corpus.chunks ?? 0).toLocaleString()} chunks`
              : 'Corpus empty — run `make ingest`'}
          </span>
        )}
        <button
          type="button"
          className="btn btn--ghost"
          onClick={onToggleTheme}
          style={{ justifyContent: 'flex-start' }}
        >
          {theme === 'dark' ? <Icon.Sun /> : <Icon.Moon />}
          {theme === 'dark' ? 'Light theme' : 'Dark theme'}
        </button>
      </div>
    </nav>
  )
}
