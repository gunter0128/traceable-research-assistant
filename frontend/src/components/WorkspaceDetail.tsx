import { useEffect, useState } from "react"
import { ArrowLeft } from "lucide-react"

import { DocumentsPanel } from "@/components/DocumentsPanel"
import { QaPanel } from "@/components/QaPanel"
import { Button } from "@/components/ui/button"
import { api, type Workspace } from "@/lib/api"

// 一個工作區的畫面：上面是標題和用量提示，左邊是文件區（DocumentsPanel），右邊是問答區（QaPanel）
export function WorkspaceDetail({
  workspace,
  onBack,
}: {
  workspace: Workspace
  onBack: () => void
}) {
  // 今天的用量到上限了沒（docs/decisions/012）
  // 問答完、文件處理完之後查一次：跨過上限的那一次當下就顯示，不用等下一次被擋才知道
  const [limitReached, setLimitReached] = useState(false)

  function refreshUsage() {
    // 查不到也沒關係，只是少顯示一個提示，不跳錯誤
    api.usageToday().then((u) => setLimitReached(u.limit_reached)).catch(() => {})
  }

  // 進入畫面時查一次
  useEffect(() => {
    refreshUsage()
  }, [])

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={onBack}>
          <ArrowLeft className="size-4" />
        </Button>
        <h1 className="text-2xl font-semibold">{workspace.name}</h1>
      </div>

      {limitReached && (
        <p className="border-destructive/50 text-destructive rounded-md border p-3 text-sm">
          今日用量已達上限，明天再試。已經在處理中的文件會繼續處理完。
        </p>
      )}

      <div className="grid gap-6 md:grid-cols-[320px_1fr]">
        <DocumentsPanel workspaceId={workspace.id} onUsageChanged={refreshUsage} />
        <QaPanel workspaceId={workspace.id} onUsageChanged={refreshUsage} />
      </div>
    </div>
  )
}
