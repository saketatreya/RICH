import { useCallback, useEffect, useState } from 'react'

import {
  api,
  type Approval,
  type Preview,
  type Run,
} from '../lib/api'

/**
 * The last step: put what was verified somewhere a person can click.
 *
 * The API has had these five routes for a while and nothing called them, so a
 * run ended at "succeeded" and the artifact stayed in the store. What makes
 * this worth its own approval rather than a button is the digest: the approval
 * returned by a request names the exact source that passed the gates, and
 * deploying checks it still matches. A change made after someone approved
 * cannot ride their decision out to a URL.
 */

interface Props {
  run: Run
  destination: string
  actor: string
}

function defaultExpiry(): string {
  // Previews are cattle. A day is long enough to show someone and short enough
  // that forgetting one is not a standing bill.
  const when = new Date(Date.now() + 24 * 60 * 60 * 1000)
  return when.toISOString().slice(0, 16)
}

export default function PreviewPanel({ run, destination, actor }: Props) {
  const [previews, setPreviews] = useState<Preview[]>([])
  const [approval, setApproval] = useState<Approval | null>(null)
  const [neonProjectId, setNeonProjectId] = useState('')
  const [vercelProjectId, setVercelProjectId] = useState('')
  const [expiresAt, setExpiresAt] = useState(defaultExpiry)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setPreviews(await api.previews(run.id))
    } catch {
      // The page's connection banner owns transient API errors.
    }
  }, [run.id])

  useEffect(() => {
    refresh()
  }, [refresh])

  const guard = async (label: string, work: () => Promise<void>) => {
    setBusy(label)
    setError(null)
    try {
      await work()
      await refresh()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy('')
    }
  }

  const latest = previews[previews.length - 1] ?? null

  return (
    <section className="v2-panel">
      <div className="v2-section-title">
        <div>
          <span className="v2-eyebrow">Preview</span>
          <h2>Deploy exactly what was verified</h2>
        </div>
        {latest && <span className={`chip ${latest.status}`}>{latest.status}</span>}
      </div>

      <p className="muted">
        Requesting a preview records an approval bound to the source digest that
        passed the gates. Deploying re-checks that digest, so a change made
        afterwards cannot travel out on an earlier decision. Credentials stay on
        the server and are never sent from this page.
      </p>

      {run.status !== 'succeeded' && (
        <p className="v2-note-warn">
          This run has not succeeded yet. Only a verified release can be
          previewed.
        </p>
      )}

      <div className="v2-preview-form">
        <label>
          <span>Neon project</span>
          <input
            value={neonProjectId}
            placeholder="neon project id"
            onChange={(event) => setNeonProjectId(event.target.value)}
          />
        </label>
        <label>
          <span>Vercel project (optional)</span>
          <input
            value={vercelProjectId}
            placeholder="vercel project id"
            onChange={(event) => setVercelProjectId(event.target.value)}
          />
        </label>
        <label>
          <span>Expires</span>
          <input
            type="datetime-local"
            value={expiresAt}
            onChange={(event) => setExpiresAt(event.target.value)}
          />
        </label>
      </div>

      <div className="v2-submit-actions">
        <button
          className="primary"
          disabled={
            !!busy || run.status !== 'succeeded' || !neonProjectId.trim()
          }
          onClick={() =>
            guard('request-preview', async () => {
              const submission = await api.requestPreview(run.id, {
                sourceDir: destination,
                neonProjectId: neonProjectId.trim(),
                expiresAt: new Date(expiresAt).toISOString(),
                vercelProjectId: vercelProjectId.trim() || undefined,
              })
              setApproval(submission.approval)
            })
          }
        >
          {busy === 'request-preview' ? 'Requesting…' : 'Request a preview'}
        </button>
        {latest && (
          <button
            className="v2-secondary"
            disabled={!!busy}
            onClick={() =>
              guard('destroy-preview', async () => {
                await api.destroyPreview(latest.id)
              })
            }
          >
            {busy === 'destroy-preview' ? 'Tearing down…' : 'Destroy preview'}
          </button>
        )}
      </div>

      {approval && approval.status === 'requested' && (
        <div className="v2-needs">
          <b>Approve this deployment</b>
          <p className="muted">
            Bound to source <code>{String(approval.request?.source_digest ?? '')}</code>
          </p>
          <div className="v2-submit-actions">
            <button
              className="primary"
              disabled={!!busy}
              onClick={() =>
                guard('deploy-preview', async () => {
                  await api.decideApproval(
                    approval.id,
                    true,
                    actor,
                    'Approved for preview deployment',
                  )
                  if (latest) await api.deployPreview(latest.id, approval.id)
                  setApproval(null)
                })
              }
            >
              {busy === 'deploy-preview' ? 'Deploying…' : 'Approve and deploy →'}
            </button>
            <button
              className="v2-secondary"
              disabled={!!busy}
              onClick={() =>
                guard('reject-preview', async () => {
                  await api.decideApproval(
                    approval.id,
                    false,
                    actor,
                    'Not deploying this build',
                  )
                  setApproval(null)
                })
              }
            >
              Reject
            </button>
          </div>
        </div>
      )}

      {error && <p className="v2-note-warn">{error}</p>}

      {previews.length > 0 && (
        <div className="v2-preview-list">
          {previews.map((preview) => (
            <article key={preview.id}>
              <div>
                <code>{preview.id}</code>
                <span className={`chip ${preview.status}`}>{preview.status}</span>
              </div>
              <small>
                source {String(preview.source_digest ?? '').slice(0, 12)} · neon{' '}
                {preview.neon_project_id}
              </small>
              {typeof preview.result?.deployment_url === 'string' && (
                <a
                  href={preview.result.deployment_url}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  {preview.result.deployment_url}
                </a>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
