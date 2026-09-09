import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_STORAGE_KEY, ThemeProvider, useTheme } from './ThemeContext';

function ThemeProbe() {
  const { theme, toggleTheme } = useTheme();
  return <button onClick={toggleTheme}>Тема: {theme}</button>;
}

function mediaPreference(initiallyDark: boolean) {
  let dark = initiallyDark;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const matchMedia = vi.fn().mockImplementation(() => ({
    get matches() { return dark; },
    media: '(prefers-color-scheme: dark)', onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
  return {
    matchMedia,
    change(next: boolean) {
      dark = next;
      listeners.forEach((listener) => listener({ matches: next } as MediaQueryListEvent));
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.colorScheme = '';
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('ThemeProvider', () => {
  it('uses the neutral light theme until the user stores an origin-local choice', () => {
    const media = mediaPreference(true);
    vi.stubGlobal('matchMedia', media.matchMedia);
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>);

    expect(screen.getByRole('button', { name: 'Тема: light' })).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Тема: light' }));
    expect(screen.getByRole('button', { name: 'Тема: dark' })).toBeInTheDocument();
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });

  it('prefers a valid stored choice over the system preference', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'light');
    const media = mediaPreference(true);
    vi.stubGlobal('matchMedia', media.matchMedia);
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>);

    expect(screen.getByRole('button', { name: 'Тема: light' })).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  });
});
