const KW = new Set([
  'def', 'class', 'return', 'import', 'from', 'if', 'elif', 'else', 'for', 'while', 'try',
  'except', 'finally', 'with', 'as', 'raise', 'in', 'not', 'and', 'or', 'is', 'lambda',
  'yield', 'pass', 'break', 'continue', 'assert', 'global', 'nonlocal', 'del', 'await', 'async',
])
const LIT = new Set(['None', 'True', 'False', 'self', 'cls'])

type Tok = { t: string; c?: string }

function tokenize(code: string): Tok[] {
  const out: Tok[] = []
  const re = /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\b\d[\d_.]*\b)|(@[A-Za-z_]\w*)|([A-Za-z_]\w*)/g
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
      out.push({ t: w, c: KW.has(w) ? 'c-kw' : LIT.has(w) ? 'c-lit' : undefined })
    }
    last = re.lastIndex
  }
  if (last < code.length) out.push({ t: code.slice(last) })
  return out
}

export default function CodeBlock({ code }: { code: string }) {
  const toks = tokenize(code)
  return (
    <pre className="codeblock">
      {toks.map((tk, i) => (tk.c ? <span key={i} className={tk.c}>{tk.t}</span> : tk.t))}
    </pre>
  )
}
