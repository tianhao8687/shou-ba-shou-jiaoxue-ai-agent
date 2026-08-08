import type { LucideIcon } from 'lucide-react'

interface MetricCardProps {
  label: string
  value: string
  detail: string
  icon: LucideIcon
  tone?: 'blue' | 'green' | 'amber' | 'teal'
}

export function MetricCard({ label, value, detail, icon: Icon, tone = 'blue' }: MetricCardProps) {
  return (
    <article className={`metric-card tone-${tone}`}>
      <span className="metric-icon"><Icon size={19} strokeWidth={1.8} aria-hidden="true" /></span>
      <div className="metric-copy">
        <span>{label}</span>
        <strong>{value}</strong>
        <small>{detail}</small>
      </div>
    </article>
  )
}

