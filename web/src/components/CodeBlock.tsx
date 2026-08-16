const PY_KW = new Set([
  'def', 'class', 'return', 'import', 'from', 'if', 'elif', 'else', 'for', 'while', 'try',
  'except', 'finally', 'with', 'as', 'raise', 'in', 'not', 'and', 'or', 'is', 'lambda',
  'yield', 'pass', 'break', 'continue', 'assert', 'global', 'nonlocal', 'del', 'await', 'async',
])
const PY_LIT = new Set(['None', 'True', 'False', 'self', 'cls'])

// v2 writes TypeScript, so the Python-only tokenizer this started as would
// paint every generated file as undifferentiated prose.
const TS_KW = new Set([
  'const', 'let', 'var', 'function', 'return', 'import', 'export', 'from', 'default',
  'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'try',
  'catch', 'finally', 'throw', 'new', 'delete', 'typeof', 'instanceof', 'in', 'of',
  'class', 'extends', 'implements', 'interface', 'type', 'enum', 'namespace', 'as',
  'async', 'await', 'yield', 'void', 'readonly', 'public', 'private', 'protected',
  'static', 'satisfies', 'keyof', 'infer', 'declare',
])
const TS_LIT = new Set([
  'null', 'undefined', 'true', 'false', 'this', 'super', 'string', 'number',
  'boolean', 'object', 'unknown', 'any', 'never',
])

export type CodeLang = 'python' | 'ts'

type Tok = { t: string; c?: string }

const PATTERNS: Record<CodeLang, RegExp> = {
  python:
    /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d[\d_.]*\b)|(@[A-Za-z_]\w*)|([A-Za-z_]\w*)/g,
  ts: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d[\d_.]*\b)|(@[A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)/g,
}

function tokenize(code: string, lang: CodeLang): Tok[] {
  const keywords = lang === 'ts' ? TS_KW : PY_KW
  const literals = lang === 'ts' ? TS_LIT : PY_LIT
  const re = new RegExp(PATTERNS[lang].source, 'g')
  const out: Tok[] = []
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(code))) {
    if (m.index > last) out.push({ t: code.slice(last, m.index) })
    if (m[1]) out.push({ t: m[1], c: 'c-com' })
    else if (m[2]) out.push({ t: m[2], c: 'c-str' })
    else if (m[3]) out.push({ t: m[3], c: 'c-num' })
    else if (m[4]) out.push({ t: m[4], c: 'c-dec' })
    else if (m[5]) {
      const w = m[5]
      out.push({ t: w, c: keywords.has(w) ? 'c-kw' : literals.has(w) ? 'c-lit' : undefined })
    }
    last = re.lastIndex
  }
  if (last < code.length) out.push({ t: code.slice(last) })
  return out
}

/** Pick a dialect from a path, so a caller does not have to. */
export function langForPath(path: string): CodeLang {
  return /\.(py|pyi)$/.test(path) ? 'python' : 'ts'
}

export default function CodeBlock({
  code,
  lang = 'python',
}: {
  code: string
  lang?: CodeLang
}) {
  const toks = tokenize(code, lang)
  return (
    <pre className="codeblock">
      {toks.map((tk, i) => (tk.c ? <span key={i} className={tk.c}>{tk.t}</span> : tk.t))}
    </pre>
  )
}
