import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { TaskStatement } from './TaskStatement';

const imageUrl = 'https://edu.mmcs.sfedu.ru/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png';
const scope = { kind: 'attempt' as const, id: 'attempt-1' };
const formula = String.raw`\sum_{k=1}^{N}\frac{1}{k\sqrt{k}}`;
const escapeAlt = (alt: string) => alt.replace(/\\/g, '\\\\').replace(/\[/g, '\\[').replace(/\]/g, '\\]');
afterEach(cleanup);

describe('safe Moodle task images', () => {
  it('renders mixed text, TeX and images in order without interpreting ordinary HTML', async () => {
    const tex = String.raw`\sum_{k=1}^{N}{ \frac{(-1)^{k}}{k(k+1)} }`;
    const { container } = render(<TaskStatement content={`Before \\[${tex}\\] middle ![A](<${imageUrl}>) after <b>literal</b>`} scope={scope} />);
    await screen.findByText('literal', { exact: false });
    // Wait for the lazy, locally bundled renderer; no CDN script is involved.
    await waitFor(() => expect(container.querySelector('math mfrac')).not.toBeNull());
    expect(container.querySelector('.task-statement__math')).toHaveAttribute('data-tex', tex);
    expect(container.querySelector('b, script')).toBeNull();
    expect(container.textContent).toContain('after <b>literal</b>');
    expect(container.querySelector('img')).toHaveAttribute('alt', 'A');
  });

  it.each([String.raw`\(x^2\)`, String.raw`\[x^2\]`, '$$x^2$$'])(
    'puts every task formula on its own line regardless of its original delimiters: %s', async (content) => {
      const { container } = render(<TaskStatement content={`Before ${content} After`} scope={scope} />);
      await waitFor(() => expect(container.querySelector('math msup')).not.toBeNull());
      const equation = container.querySelector('.task-statement__math--display');
      expect(equation).toBeInTheDocument();
      expect(equation?.previousSibling?.textContent).toBe('Before ');
      expect(equation?.nextSibling?.textContent).toBe(' After');
    },
  );

  it.each([String.raw`\\(not a formula\\)`, '$5 and $10', '\\(unclosed', `\\[${'x'.repeat(4097)}\\]`])(
    'leaves escaped, unclosed and overlong math literal: %s', (content) => {
      const { container } = render(<TaskStatement content={content} scope={scope} />);
      expect(container.querySelector('.task-statement__math')).toBeNull();
      expect(container.textContent).toBe(content);
    },
  );

  it('keeps HTML, C++ brackets, Markdown and line breaks as literal text', () => {
    const content = 'std::vector<int>; a < b; #include <iostream>\n<script>alert(1)</script>\n**not markup**';
    const { container } = render(<TaskStatement content={content} scope={scope} />);
    expect(container.firstChild?.textContent).toBe(content);
    expect(container.querySelector('script, strong, iframe, img')).toBeNull();
  });

  it('loads a formula through the authenticated scope proxy, never directly from Moodle', () => {
    const { container } = render(<TaskStatement content={`Посчитайте сумму:\n![${escapeAlt(formula)}](<${imageUrl}>)\nДля разных N.`} scope={scope} />);
    const image = screen.getByRole('img', { name: formula });
    expect(image).toHaveAttribute('src', `/api/v1/statement-images/attempt/attempt-1?url=${encodeURIComponent(imageUrl)}`);
    expect(image).toHaveAttribute('loading', 'lazy');
    expect(image).toHaveAttribute('decoding', 'async');
    expect(image).toHaveAttribute('referrerpolicy', 'no-referrer');
    expect(image).toHaveClass('task-statement__image--formula');
    expect(image).not.toHaveAttribute('tabindex');
    expect(container.querySelectorAll('img')).toHaveLength(1);
    expect(container.textContent).toBe('Посчитайте сумму:\n\nДля разных N.');
  });

  it('unescapes brackets and backslashes only inside image alt text', () => {
    const alt = String.raw`A[1] + \sqrt{x} < "test"`;
    render(<TaskStatement content={`![${escapeAlt(alt)}](<${imageUrl}>)`} scope={scope} />);
    expect(screen.getByRole('img', { name: alt })).toBeInTheDocument();
  });

  it('keeps multiple image positions in reading order', () => {
    const { container } = render(<TaskStatement content={`Первое ![A](<${imageUrl}>) затем ![B](<${imageUrl}?variant=2>) конец`} scope={scope} />);
    expect(Array.from(container.querySelector('.task-statement')!.children).map((node) => node.tagName === 'IMG' ? node.getAttribute('alt') : node.textContent)).toEqual(['Первое ', 'A', ' затем ', 'B', ' конец']);
  });

  it.each([
    '![bad](<javascript:alert(1)>)', '![bad](<data:image/svg+xml,x>)',
    '![bad](<http://moodle.test/image.png>)',
    '![bad](<file:///etc/passwd>)', '![bad](<//evil.test/image.png>)',
    '![bad](<https://user:password@evil.test/image.png>)',
    '![bad](<https://moodle.test/image.png#fragment>)',
    '![bad](<https://moodle.test/image.png#>)',
    '![bad](<https://moodle.test/image\x00.png>)',
    '![bad](<https://moodle.test/image\x1f.png>)',
    '![bad](<https://moodle.test/image\x7f.png>)',
    '![bad\x00alt](<https://moodle.test/image.png>)',
    '<img src="https://evil.test/x" onerror="alert(1)">',
    '![ordinary Markdown](https://evil.test/image.png)',
  ])('does not execute HTML or noncanonical/untrusted image syntax: %s', (content) => {
    const { container } = render(<TaskStatement content={content} scope={scope} />);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(container.firstChild?.textContent).toBe(content);
  });

  it('does not generate proxy GET URLs for overlong source URLs or image alt tokens', () => {
    const longUrl = `https://moodle.test/${'a'.repeat(4097 - 'https://moodle.test/'.length)}`;
    const longAlt = 'a'.repeat(4097);
    const content = `![long URL](<${longUrl}>)\n![${longAlt}](<${imageUrl}>)`;
    const { container } = render(<TaskStatement content={content} scope={scope} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.firstChild?.textContent).toBe(content);
  });

  it('accepts canonical URLs and escaped alt text up to the exact size bounds', () => {
    const url = `https://moodle.test/${'a'.repeat(4096 - 'https://moodle.test/'.length)}`;
    const escapedAlt = '\\\\'.repeat(2048);
    render(<TaskStatement content={`![${escapedAlt}](<${url}>)`} scope={scope} />);
    expect(screen.getByRole('img')).toHaveAttribute('alt', '\\'.repeat(2048));
    expect(screen.getByRole('img')).toHaveAttribute('src', `/api/v1/statement-images/attempt/attempt-1?url=${encodeURIComponent(url)}`);
  });

  it('interprets no more than 32 images and preserves remaining tokens as text', () => {
    const tokens = Array.from({ length: 34 }, (_, index) => `![image ${index}](<${imageUrl}?image=${index}>)`);
    const { container } = render(<TaskStatement content={tokens.join('\n')} scope={scope} />);
    expect(container.querySelectorAll('img')).toHaveLength(32);
    expect(container.textContent).toContain(tokens[32]);
    expect(container.textContent).toContain(tokens[33]);
    expect(container.querySelector('img[src*="image%3D32"]')).toBeNull();
  });

  it.each([formula, String.raw`\sum_{k=1}^{N}{ \frac{1}{k!} }`])('typesets the original TeX if Moodle cannot provide its formula image: %s', async (tex) => {
    const { container } = render(<TaskStatement content={`![${escapeAlt(tex)}](<${imageUrl}>)`} scope={scope} />);
    fireEvent.error(screen.getByRole('img', { name: tex }));
    await waitFor(() => expect(container.querySelector('math mfrac')).not.toBeNull());
    expect(container.querySelector('.task-statement__math--display')).toHaveAttribute('data-tex', tex);
    expect(container.querySelector('math')).not.toBeNull();
    expect(container.querySelector('.task-statement__math-source, .task-statement__image-fallback, img')).toBeNull();
  });

  it.each([String.raw`\[\frac{1}{k!}\]`, String.raw`\(\frac{1}{k!}\)`])('puts formula images outside the Moodle TeX filter on their own line: %s', async (tex) => {
    const { container } = render(<TaskStatement content={`![${escapeAlt(tex)}](<https://edu.mmcs.sfedu.ru/pluginfile.php/1/formula.png>)`} scope={scope} />);
    expect(screen.getByRole('img', { name: tex })).toHaveClass('task-statement__image--formula');
    fireEvent.error(screen.getByRole('img', { name: tex }));
    await waitFor(() => expect(container.querySelector('math mfrac')).not.toBeNull());
    expect(container.querySelector('.task-statement__math--display')).toHaveAttribute('data-tex', String.raw`\frac{1}{k!}`);
  });

  it('does not treat an ordinary picture caption as TeX when its image is unavailable', () => {
    const { container } = render(<TaskStatement content={'![График N + 1](<https://edu.mmcs.sfedu.ru/pluginfile.php/1/diagram.png>)'} scope={scope} />);
    expect(screen.getByRole('img', { name: 'График N + 1' })).not.toHaveClass('task-statement__image--formula');
    fireEvent.error(screen.getByRole('img', { name: 'График N + 1' }));
    expect(screen.getByRole('img', { name: 'График N + 1' })).toHaveTextContent('График N + 1');
    expect(screen.getByRole('img', { name: 'График N + 1' })).not.toHaveClass('task-statement__image--formula');
    expect(screen.getByTitle('Не удалось загрузить изображение из Moodle')).toBeInTheDocument();
    expect(container.querySelector('.task-statement__math, img')).toBeNull();
  });

  it('keeps malformed TeX readable without injecting HTML when its formula image is unavailable', async () => {
    const tex = String.raw`\notACommand{<img src=x onerror=alert(1)>}`;
    const { container } = render(<TaskStatement content={`![${escapeAlt(tex)}](<${imageUrl}>)`} scope={scope} />);
    fireEvent.error(screen.getByRole('img', { name: tex }));
    await screen.findByTitle('Не удалось отобразить формулу');
    expect(container.textContent).toBe(tex);
    expect(container.querySelector('img, script, iframe')).toBeNull();
  });

  it('keeps formula loading placeholders free of visible raw TeX', () => {
    render(<TaskStatement content={`![${escapeAlt(formula)}](<${imageUrl}>)`} scope={scope} />);
    fireEvent.error(screen.getByRole('img', { name: formula }));
    expect(document.querySelector('.task-statement__math-loading')?.textContent ?? '').toBe('');
  });

  it('provides a readable fallback without alt text and resets failure for another scope', () => {
    const page = render(<TaskStatement content={`![](<${imageUrl}>)`} scope={scope} />);
    fireEvent.error(screen.getByRole('img', { name: 'Изображение из условия задания' }));
    expect(screen.getByText('Изображение из Moodle недоступно')).toBeInTheDocument();
    page.rerender(<TaskStatement content={`![](<${imageUrl}>)`} scope={{ kind: 'submission', id: 'sub/2' }} />);
    expect(screen.getByRole('img')).toHaveAttribute('src', `/api/v1/statement-images/submission/sub%2F2?url=${encodeURIComponent(imageUrl)}`);
  });
});
