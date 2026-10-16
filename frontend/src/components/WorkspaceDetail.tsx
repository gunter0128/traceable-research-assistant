import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react"
import { ArrowLeft, FileText, RotateCw, Send, Trash2, Upload, X } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import {
  api,
  ApiError,
  type Citation,
  type Document,
  type Workspace,
} from "@/lib/api"

// 後端的 status 是給程式看的英文，畫面上換成使用者看得懂的字
const STATUS_LABELS: Record<string, string> = {
  uploaded: "排隊中",
  processing: "處理中",
  processed: "完成",
  failed: "失敗",
  cancelled: "已取消",
}

// 處理中而且知道總段數的話，顯示進度「處理中 200 / 330」
function statusText(doc: Document): string {
  const label = STATUS_LABELS[doc.status] ?? doc.status
  if (doc.status === "processing" && doc.progress_total) {
    return `${label} ${doc.progress_done ?? 0} / ${doc.progress_total}`
  }
  return label
}

// 有文件還在排隊或處理中時，每隔多久（毫秒）重新問一次後端狀態
const POLL_INTERVAL_MS = 3000

type ChatMessage = {
  role: "user" | "assistant"
  content: string
  citations?: Citation[]
}

export function WorkspaceDetail({
  workspace,
  onBack,
}: {
  workspace: Workspace
  onBack: () => void
}) {
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={onBack}>
          <ArrowLeft className="size-4" />
        </Button>
        <h1 className="text-2xl font-semibold">{workspace.name}</h1>
      </div>

      <div className="grid gap-6 md:grid-cols-[320px_1fr]">
        <DocumentsPanel workspaceId={workspace.id} />
        <QaPanel workspaceId={workspace.id} />
      </div>
    </div>
  )
}

function DocumentsPanel({ workspaceId }: { workspaceId: number }) {
  const [documents, setDocuments] = useState<Document[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    void load()
  }, [workspaceId])

  async function load() {
    setLoading(true)
    try {
      setDocuments(await api.listDocuments(workspaceId))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "讀取文件失敗")
    } finally {
      setLoading(false)
    }
  }

  // 背景處理：上傳完文件還是「處理中」，由 worker 在背景做完後改資料庫，
  // 前端沒辦法被通知，只能自己定期問。只有在真的有處理中的文件時才問，全部處理完就停
  const hasPending = documents.some((d) => d.status === "uploaded" || d.status === "processing")
  useEffect(() => {
    if (!hasPending) return
    const timer = setInterval(() => {
      // 靜默更新：不顯示「讀取中」、失敗也不跳錯誤，下一輪再問就好
      api.listDocuments(workspaceId).then(setDocuments).catch(() => {})
    }, POLL_INTERVAL_MS)
    // cleanup：hasPending 變成 false、或離開這個畫面時，把計時器關掉，不然會一直在背景問
    return () => clearInterval(timer)
  }, [hasPending, workspaceId])

  async function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const doc = await api.uploadDocument(workspaceId, file)
      setDocuments((prev) => [...prev, doc])
      if (doc.status === "failed") {
        toast.error(doc.error_message ?? "文件處理失敗")
      } else {
        toast.success("上傳成功，處理完成後即可拿來問答")
      }
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "上傳失敗")
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ""
    }
  }

  async function handleRetry(id: number) {
    try {
      const updated = await api.retryDocument(id)
      // 只換掉被重試的那一份，其他文件不動
      setDocuments((prev) => prev.map((d) => (d.id === id ? updated : d)))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "重試失敗")
    }
  }

  // 取消處理：不想等了，先停下來，文件留著之後可以重試或刪掉
  async function handleCancel(id: number) {
    try {
      const updated = await api.cancelDocument(id)
      setDocuments((prev) => prev.map((d) => (d.id === id ? updated : d)))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "取消失敗")
    }
  }

  async function handleDelete(id: number) {
    try {
      await api.deleteDocument(id)
      setDocuments((prev) => prev.filter((d) => d.id !== id))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "刪除失敗")
    }
  }

  return (
    <Card className="h-fit">
      <CardHeader>
        <CardTitle className="text-base">文件</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          className="hidden"
          onChange={(e) => void handleFileChange(e)}
        />
        <Button
          variant="outline"
          disabled={uploading}
          onClick={() => fileInputRef.current?.click()}
        >
          <Upload className="size-4" />
          {uploading ? "上傳中..." : "上傳 PDF"}
        </Button>

        <Separator />

        {loading ? (
          <p className="text-muted-foreground text-sm">讀取中...</p>
        ) : documents.length === 0 ? (
          <p className="text-muted-foreground text-sm">還沒有文件</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {documents.map((doc) => (
              <li
                key={doc.id}
                className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <FileText className="text-muted-foreground size-4 shrink-0" />
                  {/* 有訊息時在檔名下面多一行：失敗的是原因（紅字），處理中的是提示（灰字，例如「稍後會自動重試」） */}
                  <div className="flex min-w-0 flex-col">
                    <span className="truncate">{doc.filename}</span>
                    {doc.error_message && (
                      <span
                        className={
                          doc.status === "failed"
                            ? "text-destructive text-xs"
                            : "text-muted-foreground text-xs"
                        }
                      >
                        {doc.error_message}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge
                    variant={
                      doc.status === "processed"
                        ? "default"
                        : doc.status === "failed"
                          ? "destructive"
                          : "secondary"
                    }
                  >
                    {statusText(doc)}
                  </Badge>
                  {/* 排隊中、處理中：可以取消；失敗、已取消：可以重試 */}
                  {(doc.status === "uploaded" || doc.status === "processing") && (
                    <Button
                      variant="ghost"
                      size="icon"
                      title="取消處理"
                      onClick={() => void handleCancel(doc.id)}
                    >
                      <X className="size-4" />
                    </Button>
                  )}
                  {(doc.status === "failed" || doc.status === "cancelled") && (
                    <Button
                      variant="ghost"
                      size="icon"
                      title="重試"
                      onClick={() => void handleRetry(doc.id)}
                    >
                      <RotateCw className="size-4" />
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => void handleDelete(doc.id)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function QaPanel({ workspaceId }: { workspaceId: number }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [question, setQuestion] = useState("")
  const [asking, setAsking] = useState(false)
  // 目前這次問答的「取消開關」。useRef：存一個不需要觸發畫面更新的值，取消按鈕按下時拿來用
  const abortRef = useRef<AbortController | null>(null)

  async function handleAsk(e: FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (!q) return
    setMessages((prev) => [...prev, { role: "user", content: q }])
    setQuestion("")
    setAsking(true)
    // AbortController：瀏覽器內建的「取消開關」，把它的 signal 交給 fetch，之後 abort() 就能中途停掉這個請求
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const res = await api.ask(workspaceId, q, controller.signal)
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: res.answer, citations: res.citations },
      ])
    } catch (err) {
      // 使用者自己按取消的，不用跳錯誤提示
      if (!controller.signal.aborted) {
        toast.error(err instanceof ApiError ? err.message : "問答失敗")
      }
      setMessages((prev) => prev.slice(0, -1))
      // 失敗時把問題放回輸入框，使用者按一下就能重送，不用整句重打
      setQuestion(q)
    } finally {
      setAsking(false)
      abortRef.current = null
    }
  }

  return (
    <Card className="flex h-[32rem] flex-col">
      <CardHeader>
        <CardTitle className="text-base">問答</CardTitle>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3">
        <ScrollArea className="min-h-0 flex-1 pr-3">
          {messages.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              針對這個工作區裡的文件提問，答案會附上來源段落
            </p>
          ) : (
            <div className="flex flex-col gap-4">
              {messages.map((msg, i) => (
                <div key={i} className="flex flex-col gap-1.5">
                  <span className="text-muted-foreground text-xs font-medium">
                    {msg.role === "user" ? "你" : "助理"}
                  </span>
                  <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                  {msg.citations && msg.citations.length > 0 && (
                    <div className="mt-1 flex flex-col gap-1.5">
                      {msg.citations.map((c, j) => (
                        <div
                          key={j}
                          className="bg-muted rounded-md p-2 text-xs"
                        >
                          <span className="font-medium">{c.filename}</span>
                          <p className="text-muted-foreground mt-0.5 line-clamp-2">
                            {c.content}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              {/* 等待回答時顯示，問答最久可能要等 1 分鐘，畫面完全沒動靜會讓人以為當掉了 */}
              {asking && (
                <div className="flex flex-col gap-1.5">
                  <span className="text-muted-foreground text-xs font-medium">助理</span>
                  <div className="flex items-center gap-2">
                    <p className="text-muted-foreground animate-pulse text-sm">思考中…</p>
                    {/* 前端停止等待，把問題放回輸入框。後端已經送出的 OpenAI 請求收不回來，會在背景跑完 */}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-xs"
                      onClick={() => abortRef.current?.abort()}
                    >
                      取消
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}
        </ScrollArea>
        <form onSubmit={handleAsk} className="flex gap-2">
          <Textarea
            placeholder="輸入問題..."
            className="min-h-10 resize-none"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                void handleAsk(e)
              }
            }}
          />
          <Button type="submit" size="icon" disabled={asking}>
            <Send className="size-4" />
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}
