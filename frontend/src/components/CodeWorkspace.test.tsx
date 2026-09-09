import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_STORAGE_KEY, ThemeProvider, useTheme } from '../context/ThemeContext';
import { CodeWorkspace } from './CodeWorkspace';

vi.mock('@monaco-editor/react', () => ({
  default: ({ theme, options }: { theme: string; options?: { readOnlyMessage?: { value?: string } } }) => <div data-testid="monaco-theme" data-read-only-message={options?.readOnlyMessage?.value}>{theme}</div>,
}));

beforeEach(() => localStorage.clear());

function WorkspaceHarness() {
  const { toggleTheme } = useTheme();
  return <><button onClick={toggleTheme}>Сменить тему</button><CodeWorkspace
    files={[{ id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' }]}
    activeFileId="main"
    onActiveFile={() => undefined}
    onChange={() => undefined}
    scopeId="theme-test"
  /></>;
}

describe('CodeWorkspace theme', () => {
  it('uses the global dark theme for Monaco when no local override is supplied', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    render(<ThemeProvider><WorkspaceHarness /></ThemeProvider>);

    expect(screen.getByTestId('monaco-theme')).toHaveTextContent('eduprog-dark');
    fireEvent.click(screen.getByRole('button', { name: 'Сменить тему' }));
    expect(screen.getByTestId('monaco-theme')).toHaveTextContent('eduprog-light');
  });

  it('uses the supplied deletion policy for source and text data files', () => {
    const onDeleteFile = vi.fn();
    render(<ThemeProvider><CodeWorkspace
      files={[
        { id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' },
        { id: 'input', path: 'input.txt', content: '42\n', language: 'plaintext' },
      ]}
      activeFileId="main"
      onActiveFile={() => undefined}
      onChange={() => undefined}
      onDeleteFile={onDeleteFile}
      canDeleteFile={(file) => file.path.endsWith('.txt')}
      scopeId="data-file-test"
    /></ThemeProvider>);

    expect(screen.queryByRole('button', { name: 'Удалить main.cpp' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Удалить input.txt' }));
    expect(onDeleteFile).toHaveBeenCalledWith(expect.objectContaining({ id: 'input' }));
  });

  it('shows Monaco read-only feedback in Russian', () => {
    const view = render(<ThemeProvider><CodeWorkspace
      files={[{ id: 'main', path: 'main.cpp', content: 'int main() {}', language: 'cpp' }]}
      activeFileId="main"
      onActiveFile={() => undefined}
      onChange={() => undefined}
      readOnly
      scopeId="read-only-test"
    /></ThemeProvider>);

    expect(view.container.querySelector('[data-testid="monaco-theme"]')).toHaveAttribute(
      'data-read-only-message',
      'Редактирование недоступно в режиме чтения.',
    );
  });
});
