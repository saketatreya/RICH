import { useEffect, useState } from 'react'
import { ReactFlowProvider } from '@xyflow/react'
import { useStore } from './store'
import Topbar from './components/Topbar'
import CommandBar from './components/CommandBar'
import Canvas from './components/Canvas'
import Inspector from './components/Inspector'
import Dock from './components/Dock'
import BriefModal from './components/BriefModal'
import V2ControlPlane from './components/V2ControlPlane'

function LegacyCanvas() {
  const boot = useStore((s) => s.boot)
  const tree = useStore((s) => s.tree)
  const selectedId = useStore((s) => s.selectedId)

  useEffect(() => {
    boot()
  }, [boot])

  return (
    <div className="app">
      <div className={'canvas-layer' + (tree && selectedId ? ' with-inspector' : '')}>
        <ReactFlowProvider>
          <Canvas />
        </ReactFlowProvider>
      </div>

      <div className="topbar">
        <Topbar slot="brand" />
        <div className="topspacer" />
        <CommandBar />
        <div className="topspacer" />
        <Topbar slot="status" />
      </div>

      {tree && selectedId && <Inspector />}
      <Dock />
      <BriefModal />
    </div>
  )
}

export default function App() {
  const [view, setView] = useState<'v2' | 'canvas'>(() =>
    window.location.hash === '#canvas' ? 'canvas' : 'v2',
  )

  const selectView = (next: 'v2' | 'canvas') => {
    setView(next)
    window.location.hash = next === 'canvas' ? 'canvas' : 'control-plane'
  }

  return (
    <>
      {view === 'v2' ? <V2ControlPlane /> : <LegacyCanvas />}
      <nav className="view-switch glass" aria-label="RICH application view">
        <button
          className={view === 'v2' ? 'active' : ''}
          aria-pressed={view === 'v2'}
          onClick={() => selectView('v2')}
        >
          <span>◆</span> Control plane <b>v2</b>
        </button>
        <button
          className={view === 'canvas' ? 'active' : ''}
          aria-pressed={view === 'canvas'}
          onClick={() => selectView('canvas')}
        >
          <span>⌘</span> Canvas <b>v1</b>
        </button>
      </nav>
    </>
  )
}
