import { useEffect, useRef, useState } from 'react'
import { Icon } from './common'
import type { AppConfig, ProviderInfo } from '../lib/types'

/**
 * Always-visible indicator of which model is answering, and the control for
 * changing it.
 *
 * Unavailable providers are rendered DISABLED WITH THEIR REASON rather than
 * hidden. A hidden option reads as a missing feature and becomes a support
 * question; a disabled one that states "no API key configured" answers it.
 *
 * The badge is permanent because provenance changes an answer's meaning, and
 * because a user must never be unsure whether their conversation just left the
 * machine for a cloud API (PRD R5).
 */
export function ProviderBadge({
  config,
  current,
  onSelect,
  disabled,
}: {
  config: AppConfig | null
  current: { provider: string; model: string } | null
  onSelect: (provider: string) => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const anchorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    const onPointerDown = (event: MouseEvent) => {
      if (!anchorRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  if (!config) return null

  const activeName = current?.provider ?? config.active_provider
  const activeModel = current?.model ?? config.active_model
  const active = config.providers.find((p) => p.name === activeName)
  const healthy = active?.available ?? false

  return (
    <div className="anchor" ref={anchorRef}>
      <button
        type="button"
        className="provider"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={
          healthy
            ? `Answering with ${activeName} / ${activeModel}`
            : (active?.reason ?? `${activeName} is unavailable`)
        }
      >
        <span className={`dot ${healthy ? 'dot--ok' : 'dot--down'}`} aria-hidden="true" />
        <span>{activeName}</span>
        <span className="menu__meta">{activeModel}</span>
        <Icon.Chevron size={13} />
      </button>

      {open && (
        <div className="menu" role="menu" aria-label="Choose a model provider">
          <p className="menu__head">Model provider</p>

          {config.providers.map((provider) => (
            <ProviderOption
              key={provider.name}
              provider={provider}
              selected={provider.name === activeName}
              disabled={disabled || !provider.available}
              onSelect={() => {
                onSelect(provider.name)
                setOpen(false)
              }}
            />
          ))}

          <p className="menu__head">Embeddings</p>
          <div className="menu__item" style={{ cursor: 'default' }}>
            <div>
              <div className="menu__name">{config.embedding_provider}</div>
              <div className="menu__meta">{config.embedding_model}</div>
              <div className="menu__meta">
                Stays local even when chat runs in the cloud.
              </div>
            </div>
          </div>

          {disabled && (
            <p className="menu__head" style={{ textTransform: 'none' }}>
              Finish the current answer before switching.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function ProviderOption({
  provider,
  selected,
  disabled,
  onSelect,
}: {
  provider: ProviderInfo
  selected: boolean
  disabled: boolean
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={selected}
      className="menu__item"
      disabled={disabled}
      onClick={onSelect}
    >
      <span
        className={`dot ${provider.available ? 'dot--ok' : 'dot--down'}`}
        style={{ marginTop: 7 }}
        aria-hidden="true"
      />
      <span style={{ flex: 1, minWidth: 0 }}>
        <span className="menu__name">
          {provider.name}
          {selected && (
            <>
              {' '}
              <Icon.Check size={12} />
              <span className="sr-only">(selected)</span>
            </>
          )}
        </span>
        <span className="menu__meta" style={{ display: 'block' }}>
          {provider.model}
        </span>
        {!provider.available && provider.reason && (
          <span className="menu__reason" style={{ display: 'block' }}>
            {provider.reason}
          </span>
        )}
      </span>
    </button>
  )
}
