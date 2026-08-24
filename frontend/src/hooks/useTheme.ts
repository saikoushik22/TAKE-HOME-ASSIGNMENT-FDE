import { useCallback, useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'lga-theme'

function readInitial(): Theme {
  // index.html already resolved and applied the theme before first paint, so
  // read it back off the element rather than recomputing and risking a flash
  // of the wrong palette on hydration.
  const applied = document.documentElement.getAttribute('data-theme')
  return applied === 'dark' ? 'dark' : 'light'
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(readInitial)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      // Private browsing can throw on write. A non-persisted theme is a fine
      // degradation; a crash is not.
    }
  }, [theme])

  const toggle = useCallback(() => {
    setTheme((current) => (current === 'dark' ? 'light' : 'dark'))
  }, [])

  return { theme, toggle }
}
