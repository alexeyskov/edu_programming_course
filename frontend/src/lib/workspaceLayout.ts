import { useEffect, useState } from 'react';

export function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(query).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);
  return matches;
}

/** Presentation preferences only; source buffers never enter local storage. */
export function usePanelSize(key: string, initial: number, minimum: number, maximum: number) {
  const [stored, setStored] = useState(() => {
    try {
      const value = window.localStorage.getItem(key);
      const size = value === null ? initial : Number(value);
      return Number.isFinite(size) ? size : initial;
    } catch { return initial; }
  });
  const value = Math.min(maximum, Math.max(minimum, stored));
  useEffect(() => {
    const timer = window.setTimeout(() => {
      try { window.localStorage.setItem(key, String(value)); } catch { /* Layout preferences are optional. */ }
    }, 200);
    return () => window.clearTimeout(timer);
  }, [key, value]);
  return [value, (next: number) => setStored(Math.min(maximum, Math.max(minimum, next)))] as const;
}
