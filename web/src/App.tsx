import { ReactFlowProvider } from '@xyflow/react'

import ControlPlane from './components/ControlPlane'

/**
 * One product, one surface.
 *
 * This used to switch between two applications sharing a window — a control
 * plane and a canvas belonging to two different engines. There is one engine
 * now, and the canvas is part of it rather than beside it.
 */
export default function App() {
  return (
    <ReactFlowProvider>
      <ControlPlane />
    </ReactFlowProvider>
  )
}
