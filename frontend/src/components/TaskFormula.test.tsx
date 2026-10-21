import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import TaskFormula from './TaskFormula';

afterEach(cleanup);
const tex = String.raw`\sum_{k=1}^{N}{ \frac{(-1)^{k}}{k(k+1)} }`;

describe('local safe task formula rendering', () => {
  it('renders the real Moodle sum as a structured fraction with powers and limits', () => {
    const { container } = render(<TaskFormula tex={tex} display />);
    expect(container.querySelector('.katex')).toBeInTheDocument();
    expect(container.querySelector('math mfrac')).not.toBeNull();
    expect(container.querySelector('math msup')).not.toBeNull();
    expect(container.querySelector('math munderover')).not.toBeNull();
    expect(container.querySelector('annotation')?.textContent).toBe(tex);
    expect(container.querySelector('.task-statement__math--display')).toBeInTheDocument();
  });

  it.each([
    String.raw`\href{javascript:alert(1)}{click}`,
    String.raw`\includegraphics{https://evil.test/image.png}`,
    String.raw`\htmlStyle{position:fixed}{x}`,
    String.raw`\htmlClass{injected-class}{x}`,
    String.raw`\text{<img src=x onerror=alert(1)>}`,
  ])('does not create executable HTML, links, remote images or user styles: %s', (source) => {
    const { container } = render(<TaskFormula tex={source} display={false} />);
    expect(container.querySelector('script, img, a, iframe, .injected-class, [onerror]')).toBeNull();
    expect(container.querySelector('[style*="position:fixed"]')).toBeNull();
  });

  it.each([String.raw`\frac{`, String.raw`\def\a{\a}\a`, 'x'.repeat(4097)])('falls back to literal source without breaking the page: %s', (source) => {
    const { container } = render(<TaskFormula tex={source} display />);
    expect(container.querySelector('.task-statement__math-source')?.textContent).toBe(source);
    expect(container.querySelector('.katex')).toBeNull();
  });

  it('does not leak a formula macro into the next formula', () => {
    const { container } = render(<><TaskFormula tex={String.raw`\gdef\secret{123}\secret`} display={false} /><TaskFormula tex={String.raw`\secret`} display={false} /></>);
    expect(container.querySelectorAll('.katex')).toHaveLength(1);
    expect(container.querySelector('.task-statement__math-source')).toHaveTextContent(String.raw`\secret`);
  });
});
