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
  error_message: string | null
  progress_done: number | null
  progress_total: number | null
}

// 上傳許可（docs/decisions/013）：url + fields 給瀏覽器直接傳到 S3，key 是傳好之後通知 API 時要帶回去的
export type UploadTicket = {
  url: string
  fields: Record<string, string>
  key: string
}

// 答案裡的 [number] 指的是哪一份文件的哪一段（docs/decisions/021）
export type Citation = {
  number: number
  document_id: number
  filename: string
  content: string
}

export type AskResponse = {
  answer: string
  citations: Citation[]
}

// 今天（台灣時間）的用量（docs/decisions/012）。金額是字串：後端用精確小數，轉成 JSON 時變字串
export type UsageToday = {
  used_usd: string
  limit_usd: string
  limit_reached: boolean
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

  // 上傳分三步（docs/decisions/013）：檔案不經過 API，瀏覽器直接傳到 S3
  // （API 跑在 Lambda 上，一次最多只能收 6 MB，我們的上限是 20 MB）
  uploadDocument: async (workspaceId: number, file: File) => {
    // 1. 跟 API 要上傳許可：API 先檢查是不是 PDF、大小、今天的用量
    const ticket = await request<UploadTicket>(`/workspaces/${workspaceId}/uploads`, {
      method: "POST",
      body: JSON.stringify({ filename: file.name, size_bytes: file.size }),
    })

    // 2. 直接把檔案傳到 S3。許可裡的欄位（fields）要原封不動放進表單，而且 file 一定要放在最後面（S3 的規定）
    // 不走 request()：這是傳給 S3 的，不能帶我們的登入 token，S3 回的錯誤格式也跟我們的 API 不一樣
    const form = new FormData()
    for (const [name, value] of Object.entries(ticket.fields)) {
      form.append(name, value)
    }
    form.append("file", file)
    const s3Response = await fetch(ticket.url, { method: "POST", body: form })
    if (!s3Response.ok) {
      throw new ApiError(s3Response.status, "檔案上傳失敗，請重試")
    }

    // 3. 告訴 API 傳好了：API 確認檔案在 S3 上，才建紀錄、交出去處理
    return request<Document>(`/workspaces/${workspaceId}/documents`, {
      method: "POST",
      body: JSON.stringify({ key: ticket.key, filename: file.name }),
    })
  },

  retryDocument: (documentId: number) =>
    request<Document>(`/documents/${documentId}/retry`, { method: "POST" }),

  // 取消處理（docs/decisions/009）：只有排隊中、處理中的文件可以
  cancelDocument: (documentId: number) =>
    request<Document>(`/documents/${documentId}/cancel`, { method: "POST" }),

  deleteDocument: (documentId: number) =>
    request<void>(`/documents/${documentId}`, { method: "DELETE" }),

  usageToday: () => request<UsageToday>("/usage/today"),

  // signal：讓呼叫的人可以中途取消這個請求（AbortController 給的），取消時 fetch 會丟 AbortError
  ask: (workspaceId: number, question: string, signal?: AbortSignal) =>
    request<AskResponse>(`/workspaces/${workspaceId}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
      signal,
    }),
}
