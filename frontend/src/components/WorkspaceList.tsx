import { type FormEvent, useEffect, useState } from "react"
import { Trash2 } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { api, ApiError, clearToken, type Workspace } from "@/lib/api"

export function WorkspaceList({
  onOpenWorkspace,
  onLogout,
}: {
  onOpenWorkspace: (workspace: Workspace) => void
  onLogout: () => void
}) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [name, setName] = useState("")
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    void load()
  }, [])

  async function load() {
    setLoading(true)
    try {
      setWorkspaces(await api.listWorkspaces())
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "讀取工作區失敗")
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) return
    setCreating(true)
    try {
      const ws = await api.createWorkspace(name.trim())
      setWorkspaces((prev) => [...prev, ws])
      setName("")
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "建立工作區失敗")
    } finally {
      setCreating(false)
    }
  }

  async function handleDelete(id: number) {
    try {
      await api.deleteWorkspace(id)
      setWorkspaces((prev) => prev.filter((w) => w.id !== id))
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "刪除失敗")
    }
  }

  function handleLogout() {
    clearToken()
    onLogout()
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">我的工作區</h1>
          <p className="text-muted-foreground text-sm">
            每個工作區是一組獨立的文件集合，問答只會搜尋同一個工作區裡的文件
          </p>
        </div>
        <Button variant="outline" onClick={handleLogout}>
          登出
        </Button>
      </div>

      <form onSubmit={handleCreate} className="mb-6 flex gap-2">
        <Input
          placeholder="新增工作區名稱，例如：碩論相關文獻"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button type="submit" disabled={creating}>
          {creating ? "建立中..." : "建立"}
        </Button>
      </form>

      {loading ? (
        <p className="text-muted-foreground text-sm">讀取中...</p>
      ) : workspaces.length === 0 ? (
        <p className="text-muted-foreground text-sm">還沒有任何工作區，先建立一個吧</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {workspaces.map((ws) => (
            <Card
              key={ws.id}
              className="cursor-pointer transition-colors hover:border-primary"
              onClick={() => onOpenWorkspace(ws)}
            >
              <CardHeader>
                <CardTitle className="flex items-center justify-between text-base">
                  {ws.name}
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={(e) => {
                      e.stopPropagation()
                      void handleDelete(ws.id)
                    }}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </CardTitle>
                <CardDescription>
                  建立於 {new Date(ws.created_at).toLocaleDateString()}
                </CardDescription>
              </CardHeader>
              <CardContent />
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
