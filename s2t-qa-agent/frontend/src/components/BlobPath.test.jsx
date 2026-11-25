import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api.js'
import BlobPath, { blobName, describeCheck, shortPath } from './BlobPath.jsx'

vi.mock('../api.js', () => ({ api: { post: vi.fn() } }))

describe('BlobPath', () => {
  beforeEach(() => vi.clearAllMocks())

  it('names a dataset after the last part of the path', () => {
    expect(blobName('https://acme.blob.core.windows.net/raw/sales/customers.csv')).toBe('customers.csv')
    expect(blobName('abfss://raw@acme.dfs.core.windows.net/out/tx/')).toBe('tx')
    expect(blobName('https://acme.blob.core.windows.net/raw/a%20b.csv?sig=x')).toBe('a b.csv')
  })

  it('shortens storage addresses for display', () => {
    expect(shortPath('https://ACME.blob.core.windows.net/raw/in/a%20b.csv')).toBe('acme/raw/in/a b.csv')
    expect(shortPath('abfss://raw@acme.dfs.core.windows.net/out/tx/')).toBe('acme/raw/out/tx')
    expect(shortPath('something else')).toBe('something else')
  })

  it('describes a file and a folder', () => {
    expect(describeCheck({ kind: 'file', format: 'csv', total_bytes: 2048 })).toBe('One CSV file · 2.0 KB')
    expect(describeCheck({ kind: 'folder', format: 'parquet', file_count: 3, total_bytes: 3 << 20 })).toMatch(/Folder of 3 PARQUET file\(s\) · 3.0 MB · read as one table/)
  })

  it('checks the path before it can be added', async () => {
    api.post.mockResolvedValue({ kind: 'folder', format: 'parquet', file_count: 2, total_bytes: 4096, files: [{ name: 'tx/p1.parquet' }, { name: 'tx/p2.parquet' }] })
    const onAdd = vi.fn()
    render(<BlobPath onAdd={onAdd} addLabel="Add source" />)
    const add = screen.getByRole('button', { name: 'Add source' })
    expect(add).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Storage path'), { target: { value: ' abfss://raw@acme.dfs.core.windows.net/tx ' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Check' })))
    expect(api.post).toHaveBeenCalledWith('/storage/check', { path: 'abfss://raw@acme.dfs.core.windows.net/tx' })
    expect(screen.getByText(/Folder of 2 PARQUET/)).toBeInTheDocument()
    fireEvent.click(add)
    expect(onAdd).toHaveBeenCalledWith(expect.objectContaining({ path: 'abfss://raw@acme.dfs.core.windows.net/tx' }))
  })

  it('shows why a path cannot be read and keeps Add disabled', async () => {
    api.post.mockRejectedValue(new Error("This machine's identity has no read access to container 'raw'"))
    render(<BlobPath onAdd={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Storage path'), { target: { value: 'https://acme.blob.core.windows.net/raw/x.csv' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Check' })))
    expect(screen.getByText(/no read access/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
  })
})
