export const appName = import.meta.env.VITE_APP_NAME?.trim() || 'Мехмат.Практикум';

const publicBase = import.meta.env.BASE_URL;

export const sfeduLogoUrl = `${publicBase}brand/sfedu_logo.png`;
export const mmcsLogoUrl = `${publicBase}brand/mmcs_logo.png`;
