import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router'
import { LoaderCircle } from 'lucide-react'
import { useAuth } from '../context/AuthContext'

const AppShell = lazy(() => import('../components/AppShell').then((module) => ({ default: module.AppShell })))
const AuthLayout = lazy(() => import('../components/AuthLayout').then((module) => ({ default: module.AuthLayout })))
const LoginPage = lazy(() => import('../pages/auth/LoginPage').then((module) => ({ default: module.LoginPage })))
const ArenaPage = lazy(() => import('../pages/ArenaPage').then((module) => ({ default: module.ArenaPage })))
const TutorialPage = lazy(() => import('../pages/TutorialPage').then((module) => ({ default: module.TutorialPage })))
const LeaderboardPage = lazy(() => import('../pages/LeaderboardPage').then((module) => ({ default: module.LeaderboardPage })))

function RequireAuth() {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <div className="cosmic-bg grid min-h-dvh place-items-center"><LoaderCircle className="animate-spin text-cyan-signal" aria-label="Loading" /></div>
  return user ? <AppShell /> : <Navigate to="/login" state={{ from: location }} replace />
}

// The console serves operators who already play on the official site, so the
// landing route goes straight to the live arena. The tutorial gate forced
// first-visit browsers into the local practice scenario, where nothing is
// submitted upstream — mistaken for a broken arena. Training stays reachable
// from the account menu via /tutorial.
export default function App() {
  return <Suspense fallback={<div className="cosmic-bg grid min-h-dvh place-items-center"><div className="h-px w-28 overflow-hidden bg-white/10"><span className="block h-full w-1/2 animate-pulse bg-cyan-signal" /></div></div>}><Routes>
    <Route path="/demo" element={<div className="cosmic-bg min-h-dvh pt-0"><ArenaPage demo /></div>} />
    <Route path="/leaderboard" element={<LeaderboardPage />} />
    <Route element={<AuthLayout />}>
      <Route path="/login" element={<LoginPage />} />
    </Route>
    <Route element={<RequireAuth />}>
      <Route path="/" element={<ArenaPage />} />
      <Route path="/tutorial" element={<TutorialPage />} />
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Suspense>
}
