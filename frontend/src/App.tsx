import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { PageLoader, ToastProvider } from './components/ui';
import { useAuth } from './context/AuthContext';
import { AssessmentIntroPage } from './pages/PlaceholderPages';
import { CoursesPage } from './pages/CoursesPage';
import { DashboardPage } from './pages/DashboardPage';
import { IdePage } from './pages/IdePage';
import { LoginPage } from './pages/LoginPage';
import { ReviewPage } from './pages/ReviewPage';
import { SettingsPage } from './pages/SettingsPage';
import { SimilarityComparisonPage } from './pages/SimilarityComparisonPage';
import { SubmissionsPage } from './pages/SubmissionsPage';

export default function App() {
  const { session, loading, primaryRole } = useAuth();
  if (loading) return <div className="boot-screen"><span className="boot-mark">&lt;/&gt;</span><PageLoader label="Проверяем сессию…" /></div>;
  return <ToastProvider><Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route path="*" element={session ? <ProtectedRoutes primaryRole={primaryRole} systemAccess={session.capabilities.includes('SYSTEM_SETTINGS')} /> : <Navigate to="/login" replace />} />
  </Routes></ToastProvider>;
}

function ProtectedRoutes({ primaryRole, systemAccess }: { primaryRole: 'STUDENT' | 'TEACHER'; systemAccess: boolean }) {
  const canReadReviews = primaryRole === 'TEACHER' || systemAccess;
  return <Routes>
    <Route path="/" element={<AppShell><DashboardPage /></AppShell>} />
    <Route path="/courses" element={<AppShell><CoursesPage /></AppShell>} />
    <Route path="/courses/:courseId" element={<AppShell><CoursesPage /></AppShell>} />
    <Route path="/assessments/:assessmentId" element={<AppShell><AssessmentIntroPage /></AppShell>} />
    <Route path="/ide/:attemptId" element={<AppShell fullBleed><IdePage /></AppShell>} />
    <Route path="/submissions" element={canReadReviews ? <AppShell><SubmissionsPage /></AppShell> : <Navigate to="/" replace />} />
    <Route path="/review/:submissionId" element={canReadReviews ? <AppShell fullBleed><ReviewPage /></AppShell> : <Navigate to="/" replace />} />
    <Route path="/review/:submissionId/similarity/:matchId" element={canReadReviews ? <AppShell fullBleed><SimilarityComparisonPage /></AppShell> : <Navigate to="/" replace />} />
    <Route path="/task-bank" element={<Navigate to="/courses" replace />} />
    <Route path="/settings" element={<AppShell><SettingsPage /></AppShell>} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>;
}
