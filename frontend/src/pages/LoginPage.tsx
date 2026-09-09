import { ArrowRight, Check, GraduationCap, KeyRound, LockKeyhole, Shield, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { Button, Field, InlineError } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { api, demoMode } from '../lib/api';
import { appName, mmcsLogoUrl, sfeduLogoUrl } from '../lib/branding';
import { clearRememberedTokens, loadRememberedTokens, saveRememberedTokens } from '../lib/rememberedTokens';
import type { AuthConnection, Role } from '../types';

export function LoginPage() {
  const { session, devLogin, moodleLogin } = useAuth();
  const [initialRememberedTokens] = useState(loadRememberedTokens);
  const [connections, setConnections] = useState<AuthConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adminToken, setAdminToken] = useState(initialRememberedTokens.adminToken);
  const [teacherToken, setTeacherToken] = useState(initialRememberedTokens.teacherToken);
  const [hasRememberedTokens, setHasRememberedTokens] = useState(
    Boolean(initialRememberedTokens.adminToken || initialRememberedTokens.teacherToken),
  );
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<Role>('STUDENT');
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const showDev = import.meta.env.VITE_DEV_LOGIN === 'true' || demoMode !== 'never';
  const moodleConnections = connections.filter((connection) => connection.kind === 'MOODLE');
  const credentialConnections = moodleConnections.filter((connection) => connection.loginMode === 'CREDENTIALS');
  const redirectConnections = moodleConnections.filter((connection) => connection.loginMode === 'REDIRECT');

  function rememberSubmittedTokens() {
    const remembered = saveRememberedTokens({ adminToken, teacherToken });
    setHasRememberedTokens(Boolean(remembered.adminToken || remembered.teacherToken));
  }

  function forgetRememberedTokens() {
    clearRememberedTokens();
    setAdminToken('');
    setTeacherToken('');
    setHasRememberedTokens(false);
  }

  useEffect(() => {
    api.getConnections().then(setConnections).catch((caught) => setError(caught instanceof Error ? caught.message : 'Не удалось получить список LMS')).finally(() => setLoading(false));
  }, []);
  if (session) return <Navigate to={session.capabilities.includes('SYSTEM_SETTINGS') ? '/settings' : '/'} replace />;

  async function login(connectionId: string) {
    setSubmitting(true); setError(null);
    try {
      const result = await api.startLogin(connectionId, adminToken, searchParams.get('course_id') ?? undefined, teacherToken);
      rememberSubmittedTokens();
      window.location.assign(result.redirect_url);
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось начать вход'); setSubmitting(false); }
  }

  async function loginWithCredentials(connectionId: string) {
    setSubmitting(true); setError(null);
    try { await moodleLogin(connectionId, username, password, adminToken, teacherToken); rememberSubmittedTokens(); navigate(adminToken.trim() ? '/settings' : '/'); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось войти через Moodle'); }
    finally { setPassword(''); setSubmitting(false); }
  }

  async function loginDev() {
    setSubmitting(true); setError(null);
    try { await devLogin(role, adminToken); rememberSubmittedTokens(); navigate(adminToken.trim() ? '/settings' : '/'); }
    catch (caught) { setError(caught instanceof Error ? caught.message : 'Не удалось войти'); }
    finally { setSubmitting(false); }
  }

  return <div className="login-page">
    <section className="login-story">
      <div className="login-brand">
        <div className="login-brand__identity">
          <span className="login-brand__seal"><img src={sfeduLogoUrl} alt="Логотип ЮФУ" /></span>
          <span className="login-brand__copy"><strong>{appName}</strong><small>C/C++ · обучение</small></span>
        </div>
        <img className="login-brand__institute" src={mmcsLogoUrl} alt="Институт математики, механики и компьютерных наук ЮФУ" />
      </div>
      <div className="login-story__content"><h1>Среда осмысленного<br />программирования</h1><p>Рабочее пространство для самостоятельных C/C++ работ, прозрачной истории и проверки преподавателем.</p>
        <div className="login-features">
          <span><Check /> Браузерная IDE и сборка</span>
          <span><Check /> Версионированная история</span>
          <span><Check /> СППР для преподавателя</span>
          <span><Check /> ИИ помощник для студента</span>
        </div>
      </div>
      <div className="login-art" aria-hidden="true"><div className="code-card"><span>Пример · statistics.cpp</span><code><i>01</i> <b>double</b> mean(values) {'{'}<br /><i>02</i>&nbsp;&nbsp; <b>return</b> sum / size;<br /><i>03</i> {'}'}</code><small><span /> Ваш код анализируется с помощью ИИ</small></div></div>
      <small className="login-story__footer">Интеграция с LMS · Данные курса остаются под контролем университета</small>
    </section>
    <main className="login-panel">
      <div className="login-form"><span className="login-icon"><GraduationCap /></span><h2>Вход в систему</h2><p>Используйте учётную запись вашей образовательной платформы. Отдельная регистрация не требуется.</p>
        {error && <InlineError title="Не удалось войти" message={error} />}
        {!loading && credentialConnections.map((connection) => <form className="moodle-credential-login" key={connection.id} onSubmit={(event) => { event.preventDefault(); void loginWithCredentials(connection.id); }}>
          <Field label="Логин Moodle"><input autoComplete="username" maxLength={255} value={username} onChange={(event) => setUsername(event.target.value)} /></Field>
          <Field label="Пароль Moodle"><div className="input-with-icon"><LockKeyhole size={18} /><input type="password" autoComplete="current-password" maxLength={4096} value={password} onChange={(event) => setPassword(event.target.value)} /></div></Field>
          <Button type="submit" size="lg" className="login-provider" loading={submitting} disabled={!connection.enabled || !username.trim() || !password}><span className="provider-mark">M</span><span>Войти через {connection.name}</span><ArrowRight /></Button>
        </form>)}
        {!loading && redirectConnections.map((connection) => <Button key={connection.id} size="lg" className="login-provider" loading={submitting} disabled={!connection.enabled} onClick={() => void login(connection.id)}><span className="provider-mark">M</span><span>Войти через {connection.name}</span><ArrowRight /></Button>)}
        {!loading && !moodleConnections.length && !showDev && <InlineError message="Подключение Moodle для входа пока не настроено." />}
        <div className="divider"><span>дополнительно</span></div>
        <Field label="Токен преподавателя" hint="После успешного входа сохраняется только в этом браузере и подставляется при следующих входах."><div className="input-with-icon"><KeyRound size={18} /><input type="password" autoComplete="off" data-1p-ignore maxLength={512} spellCheck={false} value={teacherToken} onChange={(event) => setTeacherToken(event.target.value)} placeholder="Оставьте пустым для входа студента" /></div></Field>
        <Field label="Токен администратора" hint="После успешного входа сохраняется только в этом браузере и позволяет повторно восстановить системный доступ."><div className="input-with-icon"><KeyRound size={18} /><input type="password" autoComplete="off" data-1p-ignore maxLength={1024} spellCheck={false} value={adminToken} onChange={(event) => setAdminToken(event.target.value)} placeholder="Оставьте пустым для обычного входа" /></div></Field>
        {hasRememberedTokens && <div className="login-remembered-tokens"><small>На этом устройстве есть сохранённые токены доступа.</small><Button type="button" size="sm" variant="ghost" onClick={forgetRememberedTokens}><Trash2 size={14} /> Забыть сохранённые токены</Button></div>}
        {showDev && <div className="dev-login"><div><Shield size={17} /><span><strong>Локальная демонстрация</strong><small>Без обращения к Moodle</small></span></div><div className="segmented"><button className={role === 'STUDENT' ? 'is-active' : ''} onClick={() => setRole('STUDENT')}>Студент</button><button className={role === 'TEACHER' ? 'is-active' : ''} onClick={() => setRole('TEACHER')}>Преподаватель</button></div><Button variant="secondary" loading={submitting} onClick={() => void loginDev()}>Открыть демо</Button></div>}
        <div className="login-security"><LockKeyhole size={17} /><p>Пароль Moodle используется один раз изолированным браузерным коннектором и никогда не сохраняется. Токены доступа сохраняются в localStorage этого браузера; используйте эту возможность только на доверенном устройстве.</p></div>
      </div>
    </main>
  </div>;
}
