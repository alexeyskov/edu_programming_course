import { AlertCircle, CheckCircle2, Info, LoaderCircle, X } from 'lucide-react';
import { createContext, useCallback, useContext, useState, type ButtonHTMLAttributes, type HTMLAttributes, type ReactNode } from 'react';
import { cn } from '../lib/utils';
import { createUuid } from '../lib/uuid';

export function Button({ className, variant = 'primary', size = 'md', loading, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'sm' | 'md' | 'lg' | 'icon'; loading?: boolean }) {
  return <button className={cn('button', `button--${variant}`, `button--${size}`, className)} disabled={props.disabled || loading} {...props}>
    {loading && <LoaderCircle size={16} className="spin" aria-hidden="true" />}{children}
  </button>;
}

export function Badge({ children, tone = 'neutral', className }: { children: ReactNode; tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'purple'; className?: string }) {
  return <span className={cn('badge', `badge--${tone}`, className)}>{children}</span>;
}

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('card', className)} {...props} />;
}

export function PageLoader({ label = 'Загружаем данные…' }: { label?: string }) {
  return <div className="page-loader"><LoaderCircle className="spin" size={24} /><span>{label}</span></div>;
}

export function InlineError({ message, retry, title = 'Не получилось загрузить данные' }: { message: string; retry?: () => void; title?: string }) {
  return <div className="inline-error" role="alert"><AlertCircle size={19} /><div><strong>{title}</strong><p>{message}</p></div>{retry && <Button variant="secondary" size="sm" onClick={retry}>Повторить</Button>}</div>;
}

export function EmptyState({ icon, title, text, action }: { icon?: ReactNode; title: string; text: string; action?: ReactNode }) {
  return <div className="empty-state">{icon}<h3>{title}</h3><p>{text}</p>{action}</div>;
}

export function Field({ label, hint, error, children, className }: { label: string; hint?: string; error?: string; children: ReactNode; className?: string }) {
  return <label className={cn('field', className)}><span className="field__label">{label}</span>{children}{error ? <span className="field__error">{error}</span> : hint ? <span className="field__hint">{hint}</span> : null}</label>;
}

export function Toggle({ checked, onChange, label, description, disabled }: { checked: boolean; onChange(value: boolean): void; label: string; description?: string; disabled?: boolean }) {
  return <label className={cn('toggle-row', disabled && 'is-disabled')}><span><strong>{label}</strong>{description && <small>{description}</small>}</span><input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span className="toggle" aria-hidden="true" /></label>;
}

type ToastKind = 'success' | 'error' | 'info';
interface Toast { id: string; kind: ToastKind; title: string; text?: string }
const ToastContext = createContext<{ push(kind: ToastKind, title: string, text?: string): void } | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const remove = useCallback((id: string) => setToasts((items) => items.filter((item) => item.id !== id)), []);
  const push = useCallback((kind: ToastKind, title: string, text?: string) => {
    const id = createUuid();
    setToasts((items) => [...items, { id, kind, title, text }]);
    window.setTimeout(() => remove(id), 4500);
  }, [remove]);
  return <ToastContext.Provider value={{ push }}>{children}<div className="toast-stack" aria-live="polite">
    {toasts.map((toast) => <div key={toast.id} className={cn('toast', `toast--${toast.kind}`)}>
      {toast.kind === 'success' ? <CheckCircle2 /> : toast.kind === 'error' ? <AlertCircle /> : <Info />}
      <div><strong>{toast.title}</strong>{toast.text && <p>{toast.text}</p>}</div>
      <button onClick={() => remove(toast.id)} aria-label="Закрыть"><X size={16} /></button>
    </div>)}
  </div></ToastContext.Provider>;
}

export function useToast() {
  const value = useContext(ToastContext);
  if (!value) throw new Error('useToast must be used inside ToastProvider');
  return value;
}

export function Modal({ open, title, children, footer, onClose, width = '520px' }: { open: boolean; title: string; children: ReactNode; footer?: ReactNode; onClose(): void; width?: string }) {
  if (!open) return null;
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" style={{ maxWidth: width }}>
      <header><h2 id="modal-title">{title}</h2><button onClick={onClose} aria-label="Закрыть"><X size={20} /></button></header>
      <div className="modal__content">{children}</div>{footer && <footer>{footer}</footer>}
    </section>
  </div>;
}
