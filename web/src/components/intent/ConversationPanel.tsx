import { useState } from 'react'

import type { InterviewDocument } from '../../lib/api'
import Waiting from '../Waiting'

/**
 * The left half of the interview: what was said, what the interviewer asked,
 * and a place to say more. The draft it produces lives on the right; this
 * panel never shows an id or a step as data.
 */
export default function ConversationPanel({
  document,
  busy,
  busySince,
  onSend,
}: {
  document: InterviewDocument
  busy: string
  busySince: number
  onSend: (message: string) => void
}) {
  const [message, setMessage] = useState('')
  const transcript = document.transcript ?? []
  const outcome = document.outcome
  const sending = busy === 'interview-turn'

  const send = () => {
    const text = message.trim()
    if (!text || busy) return
    onSend(text)
    setMessage('')
  }

  return (
    <section className="plane-chat" aria-label="Interview conversation">
      <div className="plane-chat-log">
        {transcript.length === 0 && (
          <p className="plane-chat-empty">
            Describe what you want built, in your own words. The interviewer asks
            what it needs to know and drafts the specification on the right — as
            requirements and scenarios you can read, edit and approve.
          </p>
        )}
        {transcript.map((line, index) => (
          <div className={`plane-chat-line ${line.role}`} key={index}>
            <span>{line.role === 'user' ? 'You' : 'Interviewer'}</span>
            <p>{line.text}</p>
          </div>
        ))}
        {outcome?.status === 'questions' && outcome.questions.length > 0 && (
          <div className="plane-questions">
            <b>Before drafting, the interviewer asks</b>
            <ol>
              {outcome.questions.map((question, index) => (
                <li key={index}>
                  <span>{question.prompt}</span>
                  <small>{question.why}</small>
                </li>
              ))}
            </ol>
          </div>
        )}
        {outcome?.status === 'partial' && outcome.rejections.length > 0 && (
          <div className="plane-questions warn">
            <b>The draft on the right is not complete yet</b>
            <ul>
              {outcome.rejections.map((rejection, index) => (
                <li key={index}>{rejection}</li>
              ))}
            </ul>
            <small>Fix it in the editor, or say more and send again.</small>
          </div>
        )}
        {outcome?.source === 'form-fallback' && (
          <p className="plane-chat-note">
            No model route is configured on this server, so these are the fixed
            questions. The draft on the right is yours to fill in.
          </p>
        )}
        {sending && (
          <Waiting
            since={busySince}
            what="The interviewer is thinking"
            typical="one bounded model call, usually under a minute"
          />
        )}
      </div>
      <div className="plane-chat-input">
        <textarea
          aria-label="Your message to the interviewer"
          value={message}
          rows={3}
          placeholder="A task tracker for my team of eight. People sign in, create tasks with a due date, mark them done…"
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              send()
            }
          }}
        />
        <button
          type="button"
          className="primary"
          disabled={!!busy || !message.trim()}
          onClick={send}
        >
          {sending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </section>
  )
}
