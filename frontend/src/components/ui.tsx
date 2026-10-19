import { AlertCircle, CheckCircle2, Info, LoaderCircle, X } from 'lucide-react';
import { Children, cloneElement, createContext, isValidElement, useCallback, useContext, useId, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type HTMLAttributes, type ReactElement, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../lib/utils';
import { createUuid } from '../lib/uuid';

export function Button({ className, variant = 'primary', size = 'md', loading, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'sm' | 'md' | 'lg' | 'icon'; loading?: boolean }) {
  return <button className={cn('button', `button--${variant}`, `button--${size}`, className)} {...props} disabled={props.disabled || loading}>
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
  const id = useId(); const labelId = `${id}-label`; const descriptionId = `${id}-description`;
  function controls(content: ReactNode): ReactNode {
    return Children.map(content, (child) => {
      if (!isValidElement(child)) return child;
      const element = child as ReactElement<Record<string, unknown> & { children?: ReactNode }>;
      if (typeof element.type === 'string' && ['input', 'textarea', 'select'].includes(element.type)) {
        return cloneElement(element, {
          'aria-labelledby': element.props['aria-label'] ? element.props['aria-labelledby'] : element.props['aria-labelledby'] ?? labelId,
          'aria-describedby': [element.props['aria-describedby'], (hint || error) && descriptionId].filter(Boolean).join(' ') || undefined,
          'aria-invalid': error ? true : element.props['aria-invalid'],
        });
      }
      return element.props.children ? cloneElement(element, {}, controls(element.props.children)) : element;
    });
  }
  return <label className={cn('field', className)}><span id={labelId} className="field__label">{label}</span>{controls(children)}{error ? <span id={descriptionId} className="field__error">{error}</span> : hint ? <span id={descriptionId} className="field__hint">{hint}</span> : null}</label>;
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
    if (kind !== 'error') window.setTimeout(() => remove(id), 5500);
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

const modalStack: HTMLElement[] = [];
const originalInert = new Map<HTMLElement, boolean>();
let originalOverflow = '';
const focusableSelector = 'button:not(:disabled), a[href], input:not(:disabled):not([type="hidden"]), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

function modalBackground() {
  const top = modalStack.at(-1);
  if (!top) {
    originalInert.forEach((wasInert, element) => element.toggleAttribute('inert', wasInert));
    originalInert.clear();
    document.body.style.overflow = originalOverflow;
    return;
  }
  for (const element of Array.from(document.body.children)) {
    if (!(element instanceof HTMLElement)) continue;
    if (!originalInert.has(element)) originalInert.set(element, element.hasAttribute('inert'));
    element.toggleAttribute('inert', element !== top);
  }
}

export function Modal({ open, title, children, footer, onClose, width = '520px' }: { open: boolean; title: string; children: ReactNode; footer?: ReactNode; onClose(): void; width?: string }) {
  const titleId = useId();
  const overlay = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const close = useRef(onClose); close.current = onClose;
  useLayoutEffect(() => {
    if (!open || !overlay.current || !dialog.current) return;
    const container = overlay.current;
    const panel = dialog.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!modalStack.length) originalOverflow = document.body.style.overflow;
    modalStack.push(container); modalBackground(); document.body.style.overflow = 'hidden';
    const targets = () => Array.from(panel.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) =>
      !element.closest('[hidden], [inert]') && getComputedStyle(element).display !== 'none' && getComputedStyle(element).visibility !== 'hidden');
    const first = panel.querySelector<HTMLElement>('[autofocus], input:not(:disabled):not([type="hidden"]), textarea:not(:disabled)') ?? targets()[0] ?? panel;
    first.focus();
    const keydown = (event: KeyboardEvent) => {
      if (modalStack.at(-1) !== container) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (event.key !== 'Tab') return;
      const elements = targets();
      const index = elements.indexOf(document.activeElement as HTMLElement);
      if (!elements.length) { event.preventDefault(); panel.focus(); }
      else if (event.shiftKey && index <= 0) { event.preventDefault(); elements.at(-1)!.focus(); }
      else if (!event.shiftKey && (index < 0 || index === elements.length - 1)) { event.preventDefault(); elements[0].focus(); }
    };
    const focus = (event: FocusEvent) => {
      if (modalStack.at(-1) === container && !panel.contains(event.target as Node)) (targets()[0] ?? panel).focus();
    };
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('focusin', focus);
    return () => {
      document.removeEventListener('keydown', keydown, true); document.removeEventListener('focusin', focus);
      const index = modalStack.indexOf(container); if (index >= 0) modalStack.splice(index, 1);
      modalBackground();
      if (previous?.isConnected && !previous.closest('[inert]')) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(<div ref={overlay} className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialog} className="modal" role="dialog" tabIndex={-1} aria-modal="true" aria-labelledby={titleId} style={{ maxWidth: width }}>
      <header><h2 id={titleId}>{title}</h2><button type="button" onClick={onClose} aria-label="Закрыть"><X size={20} /></button></header>
      <div className="modal__content">{children}</div>{footer && <footer>{footer}</footer>}
    </section>
  </div>, document.body);
}
