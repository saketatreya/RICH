import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'

export default function Dock() {
  const job = useStore((s) => s.job)
  const [open, setOpen] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)

  // auto-open while a job runs
  useEffect(() => {
    if (job.status === 'running') setOpen(true)
  }, [job.status])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [job.log, open])

  const color =
    job.status === 'running' ? 'var(--building)' : job.status === 'error' ? 'var(--failed)' : job.status === 'done' ? 'var(--verified)' : 'var(--ink-faint)'
  const label =
    job.status === 'running' ? `${job.kind}…` : job.status === 'error' ? 'error' : job.status === 'done' ? 'done' : 'engine idle'

  return (
    <div className={'dock glass' + (open ? ' open' : '')}>
      <div className="dock-bar" onClick={() => setOpen((o) => !o)}>
        <span className={'dot' + (job.status === 'running' ? ' pulse' : '')} style={{ background: color, color }} />
        <span className="label">{label}</span>
        {job.activeNode && job.status === 'running' && <span className="active">{job.activeNode}</span>}
        <span className="sp" />
        <span className="faint" style={{ fontSize: 11 }}>console {open ? '▾' : '▴'}</span>
      </div>
      <div className="dock-log" ref={logRef}>
        {job.log.length ? job.log.join('\n') : 'output streams here when you Plan / Vibe / Build…'}
      </div>
    </div>
  )
}
