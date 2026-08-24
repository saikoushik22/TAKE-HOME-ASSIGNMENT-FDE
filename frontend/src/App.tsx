import { useCallback, useEffect, useState } from 'react'
import { ArtifactViewer } from './components/ArtifactViewer'
import { ChatPane } from './components/ChatPane'
import { ProviderBadge } from './components/ProviderBadge'
import { Sidebar } from './components/Sidebar'
import { ErrorCard, Icon } from './components/common'
import { useChat } from './hooks/useChat'
import { useTheme } from './hooks/useTheme'
import { ApiError, api } from './lib/api'
import type { AppConfig, ApiErrorBody, Artifact, SessionSummary } from './lib/types'

export default function App() {
  const { theme, toggle } = useTheme()

  const [config, setConfig] = useState<AppConfig | null>(null)
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [active, setActive] = useState<{ provider: string; model: string; title: string } | null>(null)

  const [artifact, setArtifact] = useState<Artifact | null>(null)
  const [artifactOpen, setArtifactOpen] = useState(false)
  const [generatingArtifact, setGeneratingArtifact] = useState(false)

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [mobileTab, setMobileTab] = useState<'chat' | 'artifact'>('chat')
  const [bootError, setBootError] = useState<ApiErrorBody | null>(null)

  const refreshSessions = useCallback(async () => {
    try {
      const { sessions: list } = await api.listSessions()
      setSessions(list)
      return list
    } catch {
      return [] as SessionSummary[]
    }
  }, [])

  const chat = useChat({
    onArtifact: (next) => {
      setArtifact(next)
      setArtifactOpen(true)
      setGeneratingArtifact(false)
      setMobileTab('artifact')
    },
    onTurnComplete: () => {
      void refreshSessions()
    },
  })

  const { loadSession, reset } = chat

  // ---- boot -------------------------------------------------------------
  useEffect(() => {
    let cancelled = false

    void (async () => {
      try {
        const [loadedConfig, list] = await Promise.all([api.config(), api.listSessions()])
        if (cancelled) return

        setConfig(loadedConfig)
        setSessions(list.sessions)

        // Land the user in a usable session immediately — no "create a chat"
        // ceremony before the product does anything (design.md 3.1).
        const existing = list.sessions[0]
        if (existing) {
          setActiveId(existing.id)
        } else {
          const created = await api.createSession()
          if (cancelled) return
          setSessions([created])
          setActiveId(created.id)
        }
      } catch (error) {
        if (cancelled) return
        setBootError(
          error instanceof ApiError
            ? error.body
            : {
                code: 'BOOT_FAILED',
                message: 'Could not start the app.',
                detail: { hint: 'Is the backend running on port 8000?' },
              },
        )
      }
    })()

    return () => {
      cancelled = true
    }
  }, [])

  // ---- load a session's history when it changes -------------------------
  useEffect(() => {
    if (!activeId) return
    let cancelled = false

    void (async () => {
      try {
        const detail = await loadSession(activeId)
        if (cancelled) return
        setActive({ provider: detail.provider, model: detail.model, title: detail.title })

        const { artifacts } = await api.listArtifacts(activeId)
        if (cancelled) return
        const latest = artifacts[0] ?? null
        setArtifact(latest)
        setArtifactOpen(Boolean(latest))
      } catch {
        if (!cancelled) reset()
      }
    })()

    return () => {
      cancelled = true
    }
  }, [activeId, loadSession, reset])

  // ---- Esc closes the artifact pane -------------------------------------
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && artifactOpen) {
        setArtifactOpen(false)
        setMobileTab('chat')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [artifactOpen])

  // ---- actions ----------------------------------------------------------
  const newChat = async () => {
    try {
      const created = await api.createSession({
        provider: active?.provider,
        model: active?.model,
      })
      setSessions((prev) => [created, ...prev])
      setActiveId(created.id)
      setArtifact(null)
      setArtifactOpen(false)
      setSidebarOpen(false)
      setMobileTab('chat')
    } catch {
      /* the sidebar still shows existing sessions; nothing is lost */
    }
  }

  const send = (message: string, skill?: string | null) => {
    if (!activeId) return
    if (skill === 'artifact' || skill?.startsWith('artifact_')) {
      setGeneratingArtifact(true)
      setArtifactOpen(true)
    }
    chat.send(activeId, message, skill)
  }

  const switchProvider = async (provider: string) => {
    if (!activeId) return
    try {
      const updated = await api.updateSession(activeId, { provider })
      setActive({ provider: updated.provider, model: updated.model, title: updated.title })
      setSessions((prev) => prev.map((s) => (s.id === updated.id ? { ...s, ...updated } : s)))
    } catch {
      /* the badge keeps showing the real current provider */
    }
  }

  const rename = async (id: string, title: string) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, title } : s)))
    try {
      await api.updateSession(id, { title })
    } catch {
      void refreshSessions()
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteSession(id)
    } catch {
      return
    }
    const remaining = sessions.filter((s) => s.id !== id)
    setSessions(remaining)
    if (id === activeId) {
      const next = remaining[0]
      if (next) setActiveId(next.id)
      else void newChat()
    }
  }

  if (bootError) {
    return (
      <div style={{ maxWidth: 560, margin: '80px auto', padding: 20 }}>
        <h1 style={{ fontSize: 20 }}>The Lenny Growth Assistant</h1>
        <ErrorCard error={bootError} onRetry={() => window.location.reload()} />
      </div>
    )
  }

  const corpusReady = config?.corpus?.ready ?? false
  const showArtifact = artifactOpen && (Boolean(artifact) || generatingArtifact)

  return (
    <div className="app" data-artifact={showArtifact ? 'open' : 'closed'}>
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        config={config}
        open={sidebarOpen}
        theme={theme}
        onNew={newChat}
        onSelect={(id) => {
          setActiveId(id)
          setSidebarOpen(false)
          setMobileTab('chat')
        }}
        onRename={rename}
        onDelete={remove}
        onClose={() => setSidebarOpen(false)}
        onToggleTheme={toggle}
      />

      {sidebarOpen && (
        <button
          type="button"
          className="scrim"
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <main className="chat" data-mobile-hidden={showArtifact && mobileTab === 'artifact'}>
        <header className="chat__head">
          <button
            type="button"
            className="btn btn--ghost btn--icon sidebar__toggle"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open conversations"
          >
            <Icon.Menu />
          </button>
          <h1 className="chat__title">{active?.title ?? 'New chat'}</h1>
          <ProviderBadge
            config={config}
            current={active}
            onSelect={switchProvider}
            disabled={chat.isStreaming}
          />
        </header>

        {showArtifact && (
          <div className="mobile-tabs" role="tablist" aria-label="View">
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={mobileTab === 'chat'}
              onClick={() => setMobileTab('chat')}
            >
              Chat
            </button>
            <button
              type="button"
              role="tab"
              className="tab"
              aria-selected={mobileTab === 'artifact'}
              onClick={() => setMobileTab('artifact')}
            >
              Artifact <span className="badge">1</span>
            </button>
          </div>
        )}

        <ChatPane
          messages={chat.messages}
          turn={chat.turn}
          isStreaming={chat.isStreaming}
          corpusReady={corpusReady}
          onSend={send}
          onStop={chat.stop}
          onRetry={chat.retry}
        />
      </main>

      {showArtifact && (
        <ArtifactViewer
          artifact={artifact}
          generating={generatingArtifact}
          mobileHidden={mobileTab === 'chat'}
          onClose={() => {
            setArtifactOpen(false)
            setMobileTab('chat')
          }}
          onRegenerate={chat.retry}
        />
      )}
    </div>
  )
}
