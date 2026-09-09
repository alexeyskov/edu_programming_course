import {
  ChevronDown, ClipboardCheck, GraduationCap, LayoutDashboard,
  LogOut, Menu, Moon, PanelLeftClose, PanelLeftOpen, Settings, ShieldCheck,
  SlidersHorizontal, Sun, X,
} from 'lucide-react';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { useTheme } from '../context/ThemeContext';
import { appName, mmcsLogoUrl, sfeduLogoUrl } from '../lib/branding';
import { loadRememberedTokens } from '../lib/rememberedTokens';
import { cn, initials } from '../lib/utils';
import { Badge, Button, Field, Modal, useToast } from './ui';

const SIDEBAR_STORAGE_KEY = 'eduprog.sidebar-collapsed.v1';

const titleByPath: Array<[RegExp, string]> = [
  [/\/ide\//, 'Рабочая область'], [/\/review\//, 'Проверка работы'], [/\/submissions/, 'Проверка'],
  [/\/courses\//, 'Курс'], [/\/courses/, 'Курсы'], [/\/settings/, 'Настройки системы'], [/\//, 'Обзор'],
];

export function AppShell({ children, fullBleed = false }: { children: ReactNode; fullBleed?: boolean }) {
  const { session, primaryRole, logout, elevate } = useAuth();
  const { theme, toggleTheme } = useTheme();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true'; } catch { return false; }
  });
  const [profileOpen, setProfileOpen] = useState(false);
  const [elevationOpen, setElevationOpen] = useState(false);
  const [adminToken, setAdminToken] = useState(() => loadRememberedTokens().adminToken);
  const [elevating, setElevating] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const elevated = session?.capabilities.includes('SYSTEM_SETTINGS') ?? false;
  const roleLabel = session?.memberships.some((item) => item.role === 'TEACHER')
    ? 'Преподаватель'
    : session?.memberships.some((item) => item.role === 'STUDENT')
      ? 'Студент'
      : elevated ? 'Администратор настроек' : 'Нет доступных курсов';
  const pageTitle = useMemo(() => titleByPath.find(([matcher]) => matcher.test(location.pathname))?.[1] ?? appName, [location.pathname]);

  useEffect(() => { setSidebarOpen(false); setProfileOpen(false); }, [location.pathname]);
  useEffect(() => {
    try { window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed)); } catch { /* Storage may be blocked. */ }
  }, [sidebarCollapsed]);

  const nav = primaryRole === 'TEACHER' ? [
    { to: '/', label: 'Обзор', icon: LayoutDashboard },
    { to: '/courses', label: 'Курсы и работы', icon: GraduationCap },
    { to: '/submissions', label: 'Проверка', icon: ClipboardCheck },
  ] : elevated ? [
    { to: '/', label: 'Обзор', icon: LayoutDashboard },
    { to: '/courses', label: 'Курсы', icon: GraduationCap },
    { to: '/submissions', label: 'Проверка', icon: ClipboardCheck },
  ] : [
    { to: '/', label: 'Обзор', icon: LayoutDashboard },
    { to: '/courses', label: 'Мои курсы', icon: GraduationCap },
  ];

  async function handleElevation() {
    setElevating(true);
    try {
      const token = adminToken;
      if (!token.trim()) return;
      await elevate(token);
      setElevationOpen(false);
      toast.push('success', 'Доступ к настройкам открыт', 'Повышение действует только в текущей сессии.');
      navigate('/settings');
    } catch (error) { toast.push('error', 'Токен не принят', error instanceof Error ? error.message : undefined); }
    finally { setElevating(false); }
  }

  return <div className={cn('app-shell', fullBleed && 'app-shell--full', sidebarCollapsed && 'app-shell--sidebar-collapsed')}>
    <aside className={cn('sidebar', sidebarOpen && 'is-open', sidebarCollapsed && 'is-collapsed')}>
      <div className="brand"><span className="brand__mark"><img src={sfeduLogoUrl} alt="" /></span><span><strong>{appName}</strong><small>C/C++ · обучение</small></span><button className="sidebar__collapse" onClick={() => setSidebarCollapsed(true)} aria-label="Скрыть основное меню" title="Скрыть основное меню"><PanelLeftClose size={17} /></button><button className="sidebar__close" onClick={() => setSidebarOpen(false)} aria-label="Закрыть меню"><X /></button></div>
      <div className="sidebar__scope"><span className="eyebrow">Режим</span><strong>{roleLabel}</strong><small>{session?.providerName}</small></div>
      <nav className="sidebar__nav" aria-label="Основная навигация">
        {nav.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => cn('nav-item', isActive && 'is-active')}><Icon size={19} /><span>{label}</span></NavLink>)}
        {elevated && <><span className="nav-caption">Администрирование</span><NavLink to="/settings" className={({ isActive }) => cn('nav-item', isActive && 'is-active')}><Settings size={19} /><span>Настройки системы</span></NavLink></>}
      </nav>
      <div className="sidebar__footer"><img className="sidebar__institution" src={mmcsLogoUrl} alt="Институт математики, механики и компьютерных наук ЮФУ" /><div className="system-mini"><span className="status-dot" /><span><strong>Сессия подключена</strong><small>Диагностика сервисов — в настройках</small></span></div></div>
    </aside>
    {sidebarCollapsed && <aside className="sidebar-rail" aria-label="Основное меню скрыто">
      <button type="button" onClick={() => setSidebarCollapsed(false)} aria-label="Показать основное меню" title="Показать основное меню"><PanelLeftOpen size={18} /></button>
      <span aria-hidden="true">Меню</span>
    </aside>}
    {sidebarOpen && <button className="sidebar-overlay" onClick={() => setSidebarOpen(false)} aria-label="Закрыть меню" />}
    <div className="app-main">
      <header className="topbar">
        <button className="topbar__menu" onClick={() => setSidebarOpen(true)} aria-label="Открыть меню"><Menu /></button>
        <div><span className="topbar__crumb">{appName} /</span><strong>{pageTitle}</strong></div>
        <div className="topbar__actions">
          {elevated && <Badge tone="purple"><ShieldCheck size={13} /> Системный доступ</Badge>}
          <button
            type="button"
            className="theme-toggle"
            onClick={toggleTheme}
            aria-label={theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'}
            title={theme === 'dark' ? 'Светлая тема' : 'Тёмная тема'}
          >{theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}</button>
          <div className="profile-wrap">
            <button className="profile-button" onClick={() => setProfileOpen((value) => !value)}><span className="avatar">{initials(session?.displayName ?? '')}</span><span className="profile-button__name"><strong>{session?.displayName}</strong><small>{roleLabel}</small></span><ChevronDown size={16} /></button>
            {profileOpen && <div className="profile-menu">
              <div><strong>{session?.displayName}</strong><small>{session?.providerName}</small></div>
              {!elevated && <button onClick={() => { setAdminToken(loadRememberedTokens().adminToken); setElevationOpen(true); }}><ShieldCheck size={16} />Токен администратора</button>}
              {elevated && <button onClick={() => navigate('/settings')}><SlidersHorizontal size={16} />Настройки системы</button>}
              <button onClick={() => void logout()}><LogOut size={16} />Выйти</button>
            </div>}
          </div>
        </div>
      </header>
      <main className={cn('page', fullBleed && 'page--full')}>{children}</main>
    </div>
    <Modal open={elevationOpen} title="Открыть настройки системы" onClose={() => setElevationOpen(false)} footer={<><Button variant="ghost" onClick={() => setElevationOpen(false)}>Отмена</Button><Button loading={elevating} disabled={!adminToken.trim()} onClick={() => void handleElevation()}>Проверить токен</Button></>}>
      <p className="modal-copy">Повышение не изменит вашу роль. Оно откроет системные настройки, все сданные работы добавленных курсов, повторную проверку, песочницу и изменение оценок. После успешной проверки токен останется скрытым в localStorage этого браузера.</p>
      <Field label="Токен администратора"><input type="password" autoComplete="off" data-1p-ignore maxLength={1024} spellCheck={false} value={adminToken} onChange={(event) => setAdminToken(event.target.value)} placeholder="Введите токен" /></Field>
    </Modal>
  </div>;
}
