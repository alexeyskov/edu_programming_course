import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { Button, Field, Modal } from './ui';

afterEach(cleanup);

function DialogFixture() {
  const [open, setOpen] = useState(false);
  const [nested, setNested] = useState(false);
  return <><button onClick={() => setOpen(true)}>Открыть форму</button><a href="#background">Фоновая ссылка</a>
    <Modal open={open} title="Форма проверки" onClose={() => setOpen(false)} footer={<Button onClick={() => setNested(true)}>Вложенное окно</Button>}>
      <label>Комментарий<input /></label>
    </Modal>
    <Modal open={nested} title="Подтверждение" onClose={() => setNested(false)} footer={<Button onClick={() => setNested(false)}>Готово</Button>}><p>Проверьте данные.</p></Modal>
  </>;
}

describe('dialog keyboard interaction', () => {
  it('keeps the field name separate from its entered text, units and hint', () => {
    render(<><Field label="Комментарий" hint="Виден студенту"><textarea defaultValue="Текст преподавателя" /></Field><Field label="Баллы"><div><input type="number" defaultValue={8} /><span>/ 10</span></div></Field></>);
    expect(screen.getByRole('textbox', { name: 'Комментарий' })).toHaveValue('Текст преподавателя');
    expect(screen.getByRole('textbox', { name: 'Комментарий' })).toHaveAccessibleDescription('Виден студенту');
    expect(screen.getByRole('spinbutton', { name: 'Баллы' })).toHaveValue(8);
  });

  it('moves focus inside, traps Tab, closes with Escape and restores the opener', () => {
    const { container } = render(<DialogFixture />);
    const opener = screen.getByRole('button', { name: 'Открыть форму' });
    opener.focus(); fireEvent.click(opener);
    const dialog = screen.getByRole('dialog', { name: 'Форма проверки' });
    expect(screen.getByLabelText('Комментарий')).toHaveFocus();
    expect(container).toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('hidden');
    const last = within(dialog).getByRole('button', { name: 'Вложенное окно' });
    last.focus(); fireEvent.keyDown(document, { key: 'Tab' });
    expect(within(dialog).getByRole('button', { name: 'Закрыть' })).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true }); expect(last).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(opener).toHaveFocus();
    expect(container).not.toHaveAttribute('inert'); expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('keeps the parent modal inert until a nested modal closes and restores all state on unmount', () => {
    const { container, unmount } = render(<DialogFixture />);
    fireEvent.click(screen.getByRole('button', { name: 'Открыть форму' }));
    const parent = screen.getByRole('dialog', { name: 'Форма проверки' });
    const nestedOpener = within(parent).getByRole('button', { name: 'Вложенное окно' });
    nestedOpener.focus(); fireEvent.click(nestedOpener);
    expect(parent.parentElement).toHaveAttribute('inert');
    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toContainElement(document.activeElement as HTMLElement);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Подтверждение' })).not.toBeInTheDocument();
    expect(parent.parentElement).not.toHaveAttribute('inert'); expect(nestedOpener).toHaveFocus();
    expect(container).toHaveAttribute('inert');
    unmount(); expect(document.body.style.overflow).not.toBe('hidden');
    expect(document.querySelector('.modal-backdrop')).not.toBeInTheDocument();
  });
});
