// Thin API client. A bearer token (when auth is enabled server-side) is read from localStorage.
function token() {
  try { return localStorage.getItem('qa_token') || '' } catch { return '' }
}

async function request(method, path, body, isForm = false) {
  const headers = {}
  const t = token()
  if (t) headers.Authorization = `Bearer ${t}`
  if (body && !isForm) headers['Content-Type'] = 'application/json'
  const res = await fetch(`/api${path}`, { method, headers, body: body ? (isForm ? body : JSON.stringify(body)) : undefined })
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    const detail = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : res.statusText
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b || {}),
  put: (p, b) => request('PUT', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p) => request('DELETE', p),
  upload: (p, form) => request('POST', p, form, true),
  async download(path, filename) {
    const headers = {}
    const t = token()
    if (t) headers.Authorization = `Bearer ${t}`
    const res = await fetch(`/api${path}`, { headers })
    if (!res.ok) throw new Error(`Download failed (${res.status})`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
  },
}
