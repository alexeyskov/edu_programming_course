import { useRef } from 'react';

export function PaneResizer({ label, edge, value, minimum, maximum, initial, onChange }: {
  label: string; edge: 'left' | 'right' | 'top'; value: number; minimum: number; maximum: number;
  initial: number; onChange(value: number): void;
}) {
  const drag = useRef<{ pointer: number; position: number; value: number }>();
  const vertical = edge !== 'top';
  const direction = edge === 'right' ? 1 : -1;
  const change = (value: number) => onChange(Math.min(maximum, Math.max(minimum, value)));
  return <div className={`pane-resizer pane-resizer--${edge}`} role="separator" tabIndex={0}
    aria-label={label} aria-orientation={vertical ? 'vertical' : 'horizontal'}
    aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={Math.round(value)}
    title={`${label}. Перетащите или используйте стрелки. Двойной щелчок — исходный размер.`}
    onPointerDown={(event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag.current = { pointer: event.pointerId, position: vertical ? event.clientX : event.clientY, value };
      event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={(event) => {
      if (drag.current?.pointer !== event.pointerId) return;
      change(drag.current.value + ((vertical ? event.clientX : event.clientY) - drag.current.position) * direction);
    }}
    onPointerUp={(event) => {
      if (drag.current?.pointer === event.pointerId) drag.current = undefined;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    }}
    onLostPointerCapture={() => { drag.current = undefined; }}
    onPointerCancel={() => { drag.current = undefined; }}
    onDoubleClick={() => change(initial)}
    onKeyDown={(event) => {
      const movement = vertical ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowUp: -1, ArrowDown: 1 };
      const delta = movement[event.key as keyof typeof movement];
      if (delta !== undefined) { event.preventDefault(); change(value + delta * direction * (event.shiftKey ? 40 : 10)); }
      if (event.key === 'Home' || event.key === 'End') { event.preventDefault(); change(event.key === 'Home' ? minimum : maximum); }
    }} />;
}
