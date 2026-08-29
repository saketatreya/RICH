import type { JsonValue } from '../lib/api'

/**
 * A contract, read as behaviour.
 *
 * The contract is the architect's actual artifact — it is where the design
 * decisions live — and it was only ever visible as JSON. Nobody designs by
 * reading `{"kind":"record","record_fields":[…]}`.
 *
 * So: operations as signatures, and the claims made about them as sentences.
 * The claims are the part worth reading twice, because each one is compiled
 * into a test that runs. A contract that says a thing here is a contract the
 * software is held to.
 */

interface ValueTypeDoc {
  kind: string
  record_fields?: { name: string; value_type: ValueTypeDoc }[]
  element?: ValueTypeDoc
  members?: string[]
  min_length?: number
  max_length?: number
  minimum?: number
  maximum?: number
  char_set?: string
}

interface OperationDoc {
  id: string
  name: string
  description?: string
  requirement_ids?: string[]
  input_type?: ValueTypeDoc | null
  output_type?: ValueTypeDoc | null
  errors?: { code: string; description?: string }[]
}

interface ObligationDoc {
  id: string
  relation: string
  subject_operation_id: string
  witness_operation_id?: string | null
  predicate_operation_id?: string | null
  guard_operation_id?: string | null
  sample_size?: number
  example?: { argument: JsonValue; result: JsonValue } | null
}

export interface ContractDoc {
  id: string
  node_id: string
  operations?: OperationDoc[]
  obligations?: ObligationDoc[]
  invariants?: { id: string; statement: string }[]
}

/** Render a value type the way a person would say it aloud. */
function describe(type: ValueTypeDoc | null | undefined): string {
  if (!type) return 'nothing'
  switch (type.kind) {
    case 'record':
      return `{ ${(type.record_fields ?? [])
        .map((field) => `${field.name}: ${describe(field.value_type)}`)
        .join(', ')} }`
    case 'list':
      return `list of ${describe(type.element)}`
    case 'optional':
      return `${describe(type.element)} or nothing`
    case 'enum':
      return (type.members ?? []).map((member) => `"${member}"`).join(' | ')
    case 'string': {
      const bounds =
        type.max_length !== undefined ? ` up to ${type.max_length}` : ''
      return `text${bounds}`
    }
    case 'integer': {
      if (type.minimum !== undefined && type.maximum !== undefined)
        return `whole number ${type.minimum}–${type.maximum}`
      return 'whole number'
    }
    case 'boolean':
      return 'true or false'
    default:
      return type.kind
  }
}

/**
 * Each relation as a claim in words. These are the sentences the property gate
 * turns into assertions, so they have to mean exactly what the gate checks.
 */
function claim(obligation: ObligationDoc, nameOf: (id: string) => string): string {
  const subject = nameOf(obligation.subject_operation_id)
  const witness = obligation.witness_operation_id
    ? nameOf(obligation.witness_operation_id)
    : null
  const predicate = obligation.predicate_operation_id
    ? nameOf(obligation.predicate_operation_id)
    : null
  const guard = obligation.guard_operation_id
    ? nameOf(obligation.guard_operation_id)
    : null

  switch (obligation.relation) {
    case 'example':
      return `${subject} maps one known input to one known result.`
    case 'total':
      return `${subject} answers for every input, or fails with a declared error.`
    case 'round_trip':
      return `${witness} undoes ${subject}: what goes in comes back out.`
    case 'idempotent':
      return `Applying ${subject} twice is the same as applying it once.`
    case 'preserves':
      return `${subject} never breaks ${predicate}${guard ? `, given ${guard}` : ''}.`
    case 'establishes':
      return `${subject} always makes ${predicate} true of its result.`
    default:
      return `${subject}: ${obligation.relation}`
  }
}

export default function Behaviour({ contract }: { contract: ContractDoc }) {
  const operations = contract.operations ?? []
  const obligations = contract.obligations ?? []
  const nameOf = (id: string) =>
    operations.find((operation) => operation.id === id)?.name ?? id

  return (
    <div className="plane-behaviour">
      <h4>Behaviour</h4>
      {operations.length === 0 && (
        <p className="muted">This component declares no operations.</p>
      )}
      {operations.map((operation) => {
        const claims = obligations.filter(
          (obligation) =>
            obligation.subject_operation_id === operation.id ||
            obligation.witness_operation_id === operation.id,
        )
        return (
          <article className="plane-operation" key={operation.id}>
            <header>
              <code>{operation.name}</code>
              <span>
                {describe(operation.input_type)} → {describe(operation.output_type)}
              </span>
            </header>
            {operation.description && <p>{operation.description}</p>}
            {(operation.errors?.length ?? 0) > 0 && (
              <p className="plane-operation-errors">
                May fail with{' '}
                {operation.errors!.map((error) => error.code).join(', ')}.
              </p>
            )}
            {claims.length > 0 && (
              <ul className="plane-claims">
                {claims.map((obligation) => (
                  <li key={obligation.id}>
                    {claim(obligation, nameOf)}
                    {obligation.sample_size ? (
                      <small> checked over {obligation.sample_size} cases</small>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </article>
        )
      })}
      {(contract.invariants?.length ?? 0) > 0 && (
        <>
          <h4>Always true</h4>
          <ul className="plane-claims">
            {contract.invariants!.map((invariant) => (
              <li key={invariant.id}>{invariant.statement}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
