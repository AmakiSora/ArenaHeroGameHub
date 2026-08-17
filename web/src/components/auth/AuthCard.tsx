import type { ReactNode } from 'react'

export function AuthCard({ eyebrow, title, subtitle, children }: { eyebrow?: string; title: string; subtitle?: string; children: ReactNode }) {
  return <section className="auth-card">
    <div className="mb-7">{eyebrow && <p className="eyebrow mb-3 text-cyan-signal">{eyebrow}</p>}<h1 className="font-display text-3xl font-semibold tracking-[-0.035em] text-zinc-100 sm:text-[2rem]">{title}</h1>{subtitle && <p className="mt-3 max-w-sm text-sm leading-6 text-zinc-400">{subtitle}</p>}</div>
    {children}
  </section>
}

export function FormField({ label, trailing, hint, error, className = '', ...props }: React.InputHTMLAttributes<HTMLInputElement> & { label: string; trailing?: ReactNode; hint?: string; error?: string }) {
  const id = props.id ?? props.name
  const hintID = hint ? `${id}-hint` : undefined
  const errorID = error ? `${id}-error` : undefined
  const describedBy = [props['aria-describedby'], hintID, errorID].filter(Boolean).join(' ') || undefined
  return <div>
    <label htmlFor={id} className="mb-2 block text-sm font-medium text-zinc-300">{label}</label>
    <div className="relative"><input {...props} id={id} aria-describedby={describedBy} aria-invalid={props['aria-invalid'] ?? Boolean(error)} className={`input ${trailing ? 'pr-12' : ''} ${className}`} />{trailing}</div>
    {hint && <p id={hintID} className="mt-2 text-xs leading-5 text-zinc-500">{hint}</p>}
    {error && <p id={errorID} role="alert" className="mt-2 text-xs leading-5 text-coral-hostile">{error}</p>}
  </div>
}

export function FormError({ message }: { message?: string }) {
  return message ? <div role="alert" className="rounded-gold border border-coral-hostile/20 bg-coral-hostile/5 px-4 py-3 text-sm text-coral-hostile">{message}</div> : null
}

export function AuthDivider({ label }: { label: string }) {
  return <div className="my-5 flex items-center gap-3 text-[10px] font-mono text-zinc-600"><span className="h-px flex-1 bg-white/10" />{label}<span className="h-px flex-1 bg-white/10" /></div>
}
