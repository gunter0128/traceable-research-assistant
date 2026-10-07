import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react"
import { ArrowLeft, FileText, Send, Trash2, Upload } from "lucide-react"
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

  async function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const doc = await api.uploadDocument(workspaceId, file)
      setDocuments((prev) => [...prev, doc])
      toast.success("上傳成功，處理完成後即可拿來問答")
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "上傳失敗")
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ""
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
                  <span className="truncate">{doc.filename}</span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <Badge variant={doc.status === "processed" ? "default" : "secondary"}>
                    {doc.status}
                  </Badge>
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

  async function handleAsk(e: FormEvent) {
    e.preventDefault()
    const q = question.trim()
    if (!q) return
    setMessages((prev) => [...prev, { role: "user", content: q }])
    setQuestion("")
    setAsking(true)
    try {
      const res = await api.ask(workspaceId, q)
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: res.answer, citations: res.citations },
      ])
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "問答失敗")
      setMessages((prev) => prev.slice(0, -1))
    } finally {
      setAsking(false)
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
