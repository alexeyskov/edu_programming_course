/** Shared by JSON requests and authenticated statement-image requests. */
export const apiBase = (import.meta.env.VITE_API_BASE ?? '/api/v1').replace(/\/$/, '');
