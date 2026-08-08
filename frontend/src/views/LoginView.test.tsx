import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { LoginView } from './LoginView'

describe('LoginView', () => {
  it('submits the selected server-side identity and never invents roles in the client', async () => {
    const onLogin = vi.fn().mockResolvedValue(undefined)
    render(<LoginView busy={false} onLogin={onLogin} />)

    fireEvent.click(screen.getByRole('radio', { name: /安全值班/ }))
    fireEvent.click(screen.getByRole('button', { name: /进入控制塔/ }))

    expect(onLogin).toHaveBeenCalledWith('security@harbor.local', 'harbor-demo-2026')
    expect(screen.getByRole('radio', { name: /安全值班/ })).toHaveAttribute('aria-checked', 'true')
  })

  it('renders authentication failures as an accessible alert', () => {
    render(<LoginView busy={false} error="invalid credentials" onLogin={vi.fn()} />)
    expect(screen.getByRole('alert')).toHaveTextContent('invalid credentials')
  })
})
