import { Eye, EyeOff, LoaderCircle } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { AuthCard, FormError, FormField } from '../../components/auth/AuthCard'
import { useAuth } from '../../context/AuthContext'
import { api } from '../../lib/api'
import { getErrorMessage } from '../../lib/errorMessage'

type LoginMode = 'import' | 'password'

// The session importer is the primary login: most operator accounts are
// OAuth-only (LINUX DO / GitHub), and the upstream OAuth redirect_uri is
// fixed to the official site, so those accounts copy their official-site
// session cookie and import it here. Email + password stays as a secondary
// tab. Either way a valid session routes manual commands into the MANUAL
// plan slot, overriding the bot's AGENT plan object by object.
export function LoginPage() {
  const { t } = useTranslation(); const { login, refresh } = useAuth(); const navigate = useNavigate()
  const [mode, setMode] = useState<LoginMode>('import')
  const [email, setEmail] = useState(''); const [password, setPassword] = useState(''); const [showPassword, setShowPassword] = useState(false)
  const [busy, setBusy] = useState(false); const [importBusy, setImportBusy] = useState(false); const [error, setError] = useState('')
  const [cookies, setCookies] = useState(''); const [csrf, setCsrf] = useState('')
  const switchMode = (next: LoginMode) => { setMode(next); setError('') }
  const submit = async (event: FormEvent) => { event.preventDefault(); setBusy(true); setError(''); try { await login(email, password); navigate('/') } catch (cause) { setError(getErrorMessage(cause)) } finally { setBusy(false) } }
  const importSession = async (event: FormEvent) => {
    event.preventDefault(); setImportBusy(true); setError('')
    try {
      await api.importSession(cookies, csrf.trim())
      if (await refresh()) navigate('/')
      else setError('SESSION_IMPORT_EXPIRED')
    } catch (cause) {
      setError(getErrorMessage(cause))
    } finally {
      setImportBusy(false)
    }
  }
  const tabClass = (active: boolean) => `flex min-h-11 items-center justify-center rounded-gold-sm px-3 text-sm font-medium transition-colors ${active ? 'bg-indigo-deep/55 text-blue-soft' : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-100'}`
  return <AuthCard
    eyebrow={t('auth.access')}
    title={mode === 'import' ? t('auth.importTitle') : t('auth.welcome')}
    subtitle={mode === 'import' ? undefined : t('auth.loginSubtitle')}
  >
    <div role="tablist" aria-label={t('auth.access')} className="mb-5 grid grid-cols-2 gap-1 rounded-gold border border-white/[.07] bg-white/[.03] p-1">
      <button type="button" role="tab" aria-selected={mode === 'import'} onClick={() => switchMode('import')} className={tabClass(mode === 'import')}>{t('auth.importTab')}</button>
      <button type="button" role="tab" aria-selected={mode === 'password'} onClick={() => switchMode('password')} className={tabClass(mode === 'password')}>{t('auth.passwordTab')}</button>
    </div>
    {mode === 'import' && <form onSubmit={(event) => void importSession(event)} className="space-y-4">
      <p className="text-xs leading-5 text-zinc-500">{t('auth.importHelp')}</p>
      <textarea
        value={cookies}
        onChange={(event) => setCookies(event.target.value)}
        placeholder={t('auth.importPlaceholder')}
        rows={4}
        spellCheck={false}
        aria-label={t('auth.importTab')}
        className="input h-auto min-h-24 w-full resize-y font-mono text-xs"
      />
      <FormField
        label={t('auth.csrfLabel')}
        name="csrf"
        autoComplete="off"
        spellCheck={false}
        required
        value={csrf}
        hint={t('auth.csrfHelp')}
        placeholder={t('auth.csrfPlaceholder')}
        onChange={(event) => setCsrf(event.target.value)}
        className="font-mono text-xs"
      />
      <FormError message={error} />
      <button disabled={importBusy || busy || !cookies.trim() || !csrf.trim()} aria-busy={importBusy} className="primary-button flex w-full items-center justify-center gap-2">{importBusy && <LoaderCircle size={16} className="animate-spin" />}{t('auth.importAction')}</button>
    </form>}
    {mode === 'password' && <form onSubmit={(event) => void submit(event)} className="space-y-4">
      <FormField label={t('auth.email')} name="email" type="email" autoComplete="email" required value={email} onChange={(event) => setEmail(event.target.value)} />
      <FormField label={t('auth.password')} name="password" type={showPassword ? 'text' : 'password'} autoComplete="current-password" required value={password} onChange={(event) => setPassword(event.target.value)} trailing={<button type="button" onClick={() => setShowPassword((current) => !current)} className="password-toggle focus-ring" aria-label={showPassword ? t('auth.hidePassword') : t('auth.showPassword')}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button>} />
      <FormError message={error} />
      <button disabled={busy || importBusy} aria-busy={busy} className="primary-button flex w-full items-center justify-center gap-2">{busy && <LoaderCircle size={16} className="animate-spin" />}{busy ? t('auth.signingIn') : t('auth.login')}</button>
    </form>}
  </AuthCard>
}
