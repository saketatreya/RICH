import { useEffect, useState, type FormEvent } from 'react'

import { api, ApiError, type RepositoryPush } from '../lib/api'

interface Props {
  runId: string
  tokenConfigured: boolean | null
}

// "owner/name" is what a person types; the server wants the remote URL.
const toRemote = (value: string): string => {
  const trimmed = value.trim()
  if (/^(https?|file):\/\//i.test(trimmed)) return trimmed
  return `https://github.com/${trimmed.replace(/^\/+|\/+$/g, '')}.git`
}

const errorText = (error: unknown): string =>
  error instanceof ApiError ? error.message : error instanceof Error ? error.message : String(error)

export function RepositoryPushPanel({ runId, tokenConfigured }: Props) {
  const [repository, setRepository] = useState('')
  const [create, setCreate] = useState(true)
  const [isPrivate, setIsPrivate] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pushes, setPushes] = useState<RepositoryPush[]>([])

  useEffect(() => {
    let live = true
    api
      .repositoryPushes(runId)
      .then((listed) => {
        if (live) setPushes(listed)
      })
      .catch(() => {
        /* the list is a convenience; the push itself reports its own errors */
      })
    return () => {
      live = false
    }
  }, [runId])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!repository.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const push = await api.pushRepository(runId, {
        remote,
        // Only a github.com repository can be created; anywhere else must exist.
        create: create && isGithub,
        private: isPrivate,
      })
      setPushes((previous) => [...previous, push])
    } catch (pushError) {
      setError(errorText(pushError))
    } finally {
      setBusy(false)
    }
  }

  const remote = toRemote(repository)
  const isGithub = /^https:\/\/github\.com\//i.test(remote)
  const needsToken = tokenConfigured === false && !/^file:\/\//i.test(remote)

  return (
    <form className="plane-push" onSubmit={submit}>
      <label className="plane-push-field">
        <span>Push to GitHub</span>
        <input
          type="text"
          value={repository}
          placeholder="owner/repository"
          spellCheck={false}
          onChange={(event) => setRepository(event.target.value)}
          aria-label="GitHub repository"
        />
      </label>
      <label className="plane-push-flag">
        <input
          type="checkbox"
          checked={create && isGithub}
          disabled={!isGithub}
          onChange={(event) => setCreate(event.target.checked)}
        />
        Create it if missing
      </label>
      <label className="plane-push-flag">
        <input
          type="checkbox"
          checked={isPrivate}
          onChange={(event) => setIsPrivate(event.target.checked)}
        />
        Private
      </label>
      <button
        type="submit"
        className="plane-button"
        disabled={busy || !repository.trim() || needsToken}
        title="Push the exact verified snapshot as one commit"
      >
        {busy ? 'Pushing…' : 'Push'}
      </button>
      {needsToken && (
        <p className="plane-push-note">
          Set <code>GITHUB_TOKEN</code> where <code>rich serve</code> runs to push to GitHub.
        </p>
      )}
      {error && <p className="plane-push-error">{error}</p>}
      {pushes.length > 0 && (
        <ul className="plane-push-list">
          {pushes.map((push) => (
            <li key={`${push.commit_sha}:${push.committed_at}`}>
              {push.repository_url ? (
                <a href={push.repository_url} target="_blank" rel="noreferrer">
                  {push.repository_url.replace('https://github.com/', '')}
                </a>
              ) : (
                <code>{push.remote}</code>
              )}{' '}
              · <code>{push.commit_sha.slice(0, 12)}</code> on <code>{push.branch}</code>
              {push.created_repository && ' · repository created'}
              {push.already_current && ' · already current'}
            </li>
          ))}
        </ul>
      )}
    </form>
  )
}
