import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AiCodeFinding, WorkspaceFile } from '../types';
import { MobileCodePreview, type MobileCodePreviewHandle } from './MobileCodePreview';

vi.mock('../lib/monacoSetup', () => ({ monaco: { editor: {
  colorize: vi.fn().mockResolvedValue(''),
  tokenize: (content: string) => content.split('\n').map(() => [{ offset: 0, type: 'keyword.cpp', language: 'cpp' }]),
} } }));

const file: WorkspaceFile = { id: 'main', path: 'main.cpp', content: 'int main() {\n  return 0;\n}', language: 'cpp' };
const finding: AiCodeFinding = { path: 'main.cpp', line: 2, severity: 'warning', message: 'Проверьте возвращаемое значение.' };
afterEach(cleanup);

describe('non-editable phone code preview', () => {
  it('has no input or editable DOM and ordinary lines are not buttons', async () => {
    const { container } = render(<MobileCodePreview file={file} />);
    const source = screen.getByRole('region', { name: 'Код main.cpp, только просмотр' });
    expect(source).toHaveTextContent('int main() {');
    expect(container.querySelector('input, textarea, [contenteditable]')).toBeNull();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    fireEvent.click(source.querySelector('[data-line="1"]')!);
    fireEvent.keyDown(source, { key: 'a' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(source).toHaveTextContent('return 0;');
    await waitFor(() => expect(container.querySelector('.mobile-code-token--keyword')).toBeInTheDocument());
  });

  it('opens only AI descriptions without any editor, chat action, input or model request', () => {
    render(<MobileCodePreview file={file} aiFindings={[finding]} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Замечания ИИ, строка 2' }));
    const dialog = screen.getByRole('dialog', { name: 'Ответ ИИ: строка 2' });
    expect(dialog).toHaveTextContent(finding.message);
    expect(dialog.querySelector('input, textarea, [contenteditable]')).toBeNull();
    expect(within(dialog).queryByText('Спросить у ИИ')).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Закрыть' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Закрыть' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows all overlapping remarks for the tapped line and preserves plain text safely', () => {
    const text = '<script>alert(1)</script> [link](command:forged)';
    render(<MobileCodePreview file={file} aiFindings={[
      { ...finding, line: 1, endLine: 2, message: text },
      { ...finding, severity: 'error' },
    ]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Замечания ИИ, строка 2' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(text);
    expect(dialog).toHaveTextContent(finding.message);
    expect(within(dialog).getByText('Ошибка')).toBeInTheDocument();
    expect(dialog.querySelector('script, a')).toBeNull();
  });

  it('rejects findings outside the file and invalid ranges instead of making code interactive', () => {
    render(<MobileCodePreview file={file} aiFindings={[
      { ...finding, path: 'other.cpp' }, { ...finding, line: 0 },
      { ...finding, line: 4 }, { ...finding, endLine: 1 },
      { ...finding, endLine: 99 }, { ...finding, line: 1.5 }, { ...finding, message: ' ' },
    ]} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('closes a remark when highlights are hidden or the file changes', () => {
    const view = render(<MobileCodePreview file={file} aiFindings={[finding]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Замечания ИИ, строка 2' }));
    view.rerender(<MobileCodePreview file={file} aiFindings={[]} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    view.rerender(<MobileCodePreview file={file} aiFindings={[finding]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Замечания ИИ, строка 2' }));
    view.rerender(<MobileCodePreview file={{ ...file, id: 'header', path: 'helper.h' }} aiFindings={[finding]} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('windows large files and reveals requested lines without focusing or editing code', async () => {
    const ref = createRef<MobileCodePreviewHandle>();
    const { container } = render(<MobileCodePreview ref={ref} file={{ ...file, content: Array.from({ length: 5000 }, (_, index) => `// line ${index + 1}`).join('\n') }} />);
    expect(container.querySelectorAll('[data-line]').length).toBeLessThan(100);
    act(() => ref.current?.revealLine(4000));
    expect(container.querySelector('[data-line="4000"]')).toHaveTextContent('// line 4000');
    expect(container.querySelectorAll('[data-line]').length).toBeLessThan(100);
    expect(container.querySelector('input, textarea, [contenteditable]')).toBeNull();
    expect(screen.getByRole('region')).not.toHaveFocus();
    await act(async () => {});
  });
});
