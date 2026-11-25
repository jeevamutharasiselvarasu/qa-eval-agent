import { useState } from 'react'
import { Cloud, Plus, Search } from 'lucide-react'
import { api } from '../api.js'
import { Alert, Btn } from './ui.jsx'

export const PATH_HINT = 'https://<account>.blob.core.windows.net/<container>/<path>  or  abfss://<container>@<account>.dfs.core.windows.net/<path>'

export const kb = (n) => n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${(n / 1024).toFixed(1)} KB`

/** The last part of a storage path, used as the default file name / alias. */
export const blobName = (p) => {
  const clean = (p || '').trim().split('?')[0].replace(/\/+$/, '')
  return decodeURIComponent(clean.slice(clean.lastIndexOf('/') + 1) || '')
}

/** "account/container/path" for display; the full address goes in a tooltip. */
export const shortPath = (p) => {
  const t = (p || '').trim()
  let m = t.match(/^https:\/\/([a-z0-9]+)\.(?:blob|dfs)\.core\.windows\.net\/(.*)$/i)
  if (m) return `${m[1].toLowerCase()}/${decodeURIComponent(m[2]).replace(/\/+$/, '')}`
  m = t.match(/^(?:abfss|wasbs):\/\/([^@]+)@([a-z0-9]+)\.(?:blob|dfs)\.core\.windows\.net\/?(.*)$/i)
  if (m) return `${m[2].toLowerCase()}/${m[1]}/${decodeURIComponent(m[3]).replace(/\/+$/, '')}`.replace(/\/$/, '')
  return t
}

export function StoragePath({ uri }) {
  return <div className="storage-path" title={uri}><Cloud size={12} /><span>{shortPath(uri)}</span></div>
}

export function describeCheck(c) {
  return c.kind === 'file'
    ? `One ${c.format.toUpperCase()} file · ${kb(c.total_bytes)}`
    : `Folder of ${c.file_count} ${c.format.toUpperCase()} file(s) · ${kb(c.total_bytes)} · read as one table`
}

/**
 * Enter an Azure Blob / ADLS Gen2 path, check it, then add it.
 * onAdd({ path, check }) is called after a successful check. `addLabel` names the button.
 */
export default function BlobPath({ onAdd, addLabel = 'Add', busy = false }) {
  const [path, setPath] = useState('')
  const [check, setCheck] = useState(null)
  const [checking, setChecking] = useState(false)

  async function run() {
    setChecking(true)
    try { setCheck({ ok: true, ...(await api.post('/storage/check', { path: path.trim() })) }) } catch (e) { setCheck({ ok: false, error: e.message }) }
    setChecking(false)
  }
  const add = () => { onAdd({ path: path.trim(), check }); setPath(''); setCheck(null) }

  return (
    <div className="blob">
      <label className="field">Azure Blob / ADLS Gen2 path
        <span className="row gap-s">
          <input className="grow mono" value={path} placeholder="https://account.blob.core.windows.net/container/folder/file.csv" aria-label="Storage path"
            onChange={e => { setPath(e.target.value); setCheck(null) }} onKeyDown={e => e.key === 'Enter' && path.trim() && run()} />
          <Btn icon={Search} busy={checking} disabled={!path.trim()} onClick={run}>Check</Btn>
          <Btn className="primary" icon={Plus} busy={busy} disabled={!check?.ok} onClick={add}>{addLabel}</Btn>
        </span>
      </label>
      {check && (check.ok
        ? <div className="mt-s"><Alert kind="ok"><Cloud size={14} /> {describeCheck(check)}
          {check.kind === 'folder' && <div className="tiny mt-s mono">{check.files.slice(0, 5).map(f => f.name).join(', ')}{check.file_count > 5 ? ` … +${check.file_count - 5} more` : ''}</div>}</Alert></div>
        : <div className="mt-s"><Alert kind="err">{check.error}</Alert></div>)}
      <div className="tiny muted mt-s">A file, or a folder of part files of one type (e.g. Spark output). Read with this server's Azure identity
        (az login or managed identity; it needs the Storage Blob Data Reader role). Don't paste SAS tokens.</div>
    </div>
  )
}
