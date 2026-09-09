import {
  createContext, useCallback, useContext, useLayoutEffect, useMemo, useState,
  type ReactNode,
} from 'react';

export type ColorTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'eduprog.color-theme.v1';

interface ThemeContextValue {
  theme: ColorTheme;
  setTheme(theme: ColorTheme): void;
  toggleTheme(): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function storedTheme(): ColorTheme | null {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    return null;
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  // Start with a neutral VS Code-like light palette.  The explicit dark-theme
  // choice remains available and is persisted for future visits.
  const [theme, setResolvedTheme] = useState<ColorTheme>(() => storedTheme() ?? 'light');

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  const setTheme = useCallback((next: ColorTheme) => {
    setResolvedTheme(next);
    try { window.localStorage.setItem(THEME_STORAGE_KEY, next); } catch { /* Storage may be blocked. */ }
  }, []);
  const toggleTheme = useCallback(() => {
    setTheme(theme === 'dark' ? 'light' : 'dark');
  }, [setTheme, theme]);
  const value = useMemo(() => ({ theme, setTheme, toggleTheme }), [setTheme, theme, toggleTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error('useTheme must be used inside ThemeProvider');
  return value;
}
