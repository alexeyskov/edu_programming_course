import { beforeEach, describe, expect, it } from 'vitest';
import {
  clearRememberedTokens,
  loadRememberedTokens,
  saveRememberedToken,
  saveRememberedTokens,
} from './rememberedTokens';

beforeEach(() => localStorage.clear());

describe('remembered access tokens', () => {
  it('persists and reloads only the two supported token values', () => {
    saveRememberedTokens({ teacherToken: 'teacher-secret', adminToken: 'admin-secret' });

    expect(loadRememberedTokens()).toEqual({
      teacherToken: 'teacher-secret',
      adminToken: 'admin-secret',
    });
  });

  it('merges one token without deleting the other and ignores empty field values', () => {
    saveRememberedTokens({ teacherToken: 'teacher-secret', adminToken: 'admin-secret' });
    saveRememberedToken('adminToken', 'new-admin-secret');
    saveRememberedTokens({ teacherToken: '', adminToken: '   ' });

    expect(loadRememberedTokens()).toEqual({
      teacherToken: 'teacher-secret',
      adminToken: 'new-admin-secret',
    });
  });

  it('rejects a teacher token longer than the backend contract allows', () => {
    localStorage.setItem('eduprog.remembered-access-tokens.v1', JSON.stringify({
      teacherToken: 'x'.repeat(513),
      adminToken: 'valid-admin',
    }));

    expect(loadRememberedTokens()).toEqual({ teacherToken: '', adminToken: 'valid-admin' });
  });

  it('rejects an admin token longer than the backend contract allows', () => {
    localStorage.setItem('eduprog.remembered-access-tokens.v1', JSON.stringify({
      teacherToken: 'valid-teacher',
      adminToken: 'x'.repeat(1025),
      password: 'must-not-be-read',
    }));

    expect(loadRememberedTokens()).toEqual({ teacherToken: 'valid-teacher', adminToken: '' });
  });

  it('removes both tokens only through the explicit clear operation', () => {
    saveRememberedTokens({ teacherToken: 'teacher-secret', adminToken: 'admin-secret' });
    clearRememberedTokens();

    expect(loadRememberedTokens()).toEqual({ teacherToken: '', adminToken: '' });
  });
});
