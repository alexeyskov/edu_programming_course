import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { CourseTitle } from './CourseTitle';

afterEach(cleanup);

it('renders a plain course title without a comment placeholder', () => {
  const { container } = render(<h2><CourseTitle title="C++" /></h2>);
  expect(container.textContent).toBe('C++');
  expect(container.querySelector('.course-title__comment')).toBeNull();
});

it('keeps comments as untrusted text in the same heading', () => {
  const { container } = render(<h2><CourseTitle title="C++" comment={'<img src=x onerror=alert(1)>'} /></h2>);
  expect(container.querySelector('img, script')).toBeNull();
  expect(container.querySelector('h2')).toHaveTextContent('C++ <img src=x onerror=alert(1)>');
});
