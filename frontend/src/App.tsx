import { useState } from "react"
import { Toaster } from "@/components/ui/sonner"
import { AuthScreen } from "@/components/AuthScreen"
import { WorkspaceDetail } from "@/components/WorkspaceDetail"
import { WorkspaceList } from "@/components/WorkspaceList"
import { getToken, type Workspace } from "@/lib/api"

// 沒用 react-router：畫面只有三種狀態，用一個變數切換就夠了，
// 不用多引入一個路由套件、也不用在部署時處理 client-side routing 的設定
type View =
  | { name: "auth" }
  | { name: "workspaces" }
  | { name: "workspace-detail"; workspace: Workspace }

function App() {
  const [view, setView] = useState<View>(
    getToken() ? { name: "workspaces" } : { name: "auth" }
  )

  return (
    <>
      {view.name === "auth" && (
        <AuthScreen onAuthed={() => setView({ name: "workspaces" })} />
      )}
      {view.name === "workspaces" && (
        <WorkspaceList
          onOpenWorkspace={(workspace) =>
            setView({ name: "workspace-detail", workspace })
          }
          onLogout={() => setView({ name: "auth" })}
        />
      )}
      {view.name === "workspace-detail" && (
        <WorkspaceDetail
          workspace={view.workspace}
          onBack={() => setView({ name: "workspaces" })}
        />
      )}
      <Toaster />
    </>
  )
}

export default App
