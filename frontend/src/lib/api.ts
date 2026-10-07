// 包所有打後端 API 的 fetch 呼叫。元件不直接呼叫 fetch，都透過這裡，
// 之後後端路徑或錯誤處理方式變了，只要改這一個檔案。

export type User = {
  id: number
  email: string
  created_at: string
}

export type Workspace = {
  id: number
  name: string
  created_at: string
  updated_at: string
}

export type Document = {
  id: number
  workspace_id: number
  filename: string
  status: string
  uploaded_at: string
  processed_at: string | null
}

export type Citation = {
  document_id: number
  filename: string
  content: string
}

export type AskResponse = {
  answer: string
  citations: Citation[]
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

const TOKEN_KEY = "trace-app-token"

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

// 統一處理「帶 token」、「把非 2xx 轉成 ApiError」這兩件事，其他 function 只要專心準備參數
async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getToken()
  const headers = new Headers(options.headers)
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json")
  }

  const res = await fetch(path, { ...options, headers })

  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const message = body?.detail ?? `request failed with status ${res.status}`
    throw new ApiError(res.status, message)
  }

  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const api = {
  register: (email: string, password: string) =>
    request<User>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  login: (email: string, password: string) =>
    request<{ access_token: string; token_type: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<User>("/auth/me"),

  listWorkspaces: () => request<Workspace[]>("/workspaces"),

  createWorkspace: (name: string) =>
    request<Workspace>("/workspaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  deleteWorkspace: (workspaceId: number) =>
    request<void>(`/workspaces/${workspaceId}`, { method: "DELETE" }),

  listDocuments: (workspaceId: number) =>
    request<Document[]>(`/workspaces/${workspaceId}/documents`),

  uploadDocument: (workspaceId: number, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return request<Document>(`/workspaces/${workspaceId}/documents`, {
      method: "POST",
      body: form,
    })
  },

  deleteDocument: (documentId: number) =>
    request<void>(`/documents/${documentId}`, { method: "DELETE" }),

  ask: (workspaceId: number, question: string) =>
    request<AskResponse>(`/workspaces/${workspaceId}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
}
