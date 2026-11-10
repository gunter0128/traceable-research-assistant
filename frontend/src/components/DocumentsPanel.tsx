import { type ChangeEvent, useEffect, useRef, useState } from "react"
import { FileText, RotateCw, Trash2, Upload } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { api, ApiError, type Document } from "@/lib/api"

// 後端的 status 是給程式看的英文，畫面上換成使用者看得懂的字
// cancelled：取消功能拿掉了（docs/decisions/046），但以前取消的文件還在，要照常顯示
const STATUS_LABELS: Record<string, string> = {
  uploaded: "排隊中",
  processing: "處理中",
  processed: "完成",
  failed: "失敗",
  cancelled: "已取消",
}

// 有文件還在排隊或處理中時，每隔多久（毫秒）重新問一次後端狀態
const POLL_INTERVAL_MS = 3000

// 還在排隊或處理中：要繼續問後端
function isPending(doc: Document) {
  return doc.status === "uploaded" || doc.status === "processing"
}

// 失敗、已取消（以前取消的）可以重試。排隊中、處理中的不想等，可以直接刪掉
function canRetry(doc: Document) {
  return doc.status === "failed" || doc.status === "cancelled"
}

// 狀態標籤的顏色：完成用主色、失敗用紅色，其他（排隊中、處理中、已取消）用灰色
function badgeVariant(status: string) {
  if (status === "processed") return "default"
  if (status === "failed") return "destructive"
  return "secondary"
}

// 左邊的文件區：上傳、列表、重試、刪除
// onUsageChanged：用量可能變了（文件處理完了），請上層重新查一次
export function DocumentsPanel({
  workspaceId,
  onUsageChanged,
}: {
  workspaceId: number
  onUsageChanged: () => void
}) {
  const [documents, setDocuments] = useState<Document[]>([])
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // 進入畫面（或換了工作區）時讀一次文件列表
  useEffect(() => {
    async function loadDocuments() {
      setLoading(true)
      try {
        setDocuments(await api.listDocuments(workspaceId))
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : "讀取文件失敗")
      } finally {
        setLoading(false)
      }
    }
    void loadDocuments()
  }, [workspaceId])

  // 背景處理：上傳完文件還是「處理中」，由 worker 在背景做完後改資料庫，
  // 前端沒辦法被通知，只能自己定期問。只有在真的有處理中的文件時才問，全部處理完就停
  const hasPending = documents.some(isPending)
  useEffect(() => {
    if (!hasPending) return
    const timer = setInterval(() => {
      // 靜默更新：不顯示「讀取中」、失敗也不跳錯誤，下一輪再問就好
      api.listDocuments(workspaceId).then(setDocuments).catch(() => {})
    }, POLL_INTERVAL_MS)
    // cleanup：hasPending 變成 false、或離開這個畫面時，把計時器關掉，不然會一直在背景問
    return () => clearInterval(timer)
  }, [hasPending, workspaceId])

  // 處理中的文件都處理完了（hasPending 從 true 變成 false）→ 轉向量花了錢，請上層查一次用量
  // wasPending：上一次檢查時有沒有處理中的文件。useRef：記住一個值，改它不會讓畫面重畫
  const wasPending = useRef(false)
  useEffect(() => {
    if (wasPending.current && !hasPending) onUsageChanged()
    wasPending.current = hasPending
  }, [hasPending, onUsageChanged])

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
      // 清掉選過的檔案：不然同一個檔案再選一次不會觸發 onChange
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
        {/* 真正的檔案選擇框藏起來，按下面的按鈕時幫它按一下 */}
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

        <DocumentList
          loading={loading}
          documents={documents}
          onRetry={handleRetry}
          onDelete={handleDelete}
        />
      </CardContent>
    </Card>
  )
}

// 文件列表：讀取中 → 提示；沒有文件 → 提示；有文件 → 一份一行
function DocumentList({
  loading,
  documents,
  onRetry,
  onDelete,
}: {
  loading: boolean
  documents: Document[]
  onRetry: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
}) {
  if (loading) {
    return <p className="text-muted-foreground text-sm">讀取中...</p>
  }
  if (documents.length === 0) {
    return <p className="text-muted-foreground text-sm">還沒有文件</p>
  }
  return (
    <ul className="flex flex-col gap-2">
      {documents.map((doc) => (
        <DocumentRow key={doc.id} doc={doc} onRetry={onRetry} onDelete={onDelete} />
      ))}
    </ul>
  )
}

// 一份文件：檔名（和訊息）、狀態標籤、重試和刪除按鈕
function DocumentRow({
  doc,
  onRetry,
  onDelete,
}: {
  doc: Document
  onRetry: (id: number) => Promise<void>
  onDelete: (id: number) => Promise<void>
}) {
  // 有訊息時在檔名下面多一行：失敗的是原因（紅字）
  // 其他狀態現在不會有訊息；舊資料可能留著以前自動重試的提示，顯示成灰字
  const messageClass = doc.status === "failed" ? "text-destructive text-xs" : "text-muted-foreground text-xs"

  return (
    <li className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm">
      <div className="flex min-w-0 items-center gap-2">
        <FileText className="text-muted-foreground size-4 shrink-0" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate">{doc.filename}</span>
          {doc.error_message && <span className={messageClass}>{doc.error_message}</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Badge variant={badgeVariant(doc.status)}>
          {STATUS_LABELS[doc.status] ?? doc.status}
        </Badge>
        {canRetry(doc) && (
          <Button
            variant="ghost"
            size="icon"
            title="重試"
            onClick={() => void onRetry(doc.id)}
          >
            <RotateCw className="size-4" />
          </Button>
        )}
        <Button variant="ghost" size="icon" onClick={() => void onDelete(doc.id)}>
          <Trash2 className="size-4" />
        </Button>
      </div>
    </li>
  )
}
