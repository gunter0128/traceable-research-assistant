import { type FormEvent, type KeyboardEvent, useRef, useState } from "react"
import { Send } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Textarea } from "@/components/ui/textarea"
import { api, ApiError, type Citation } from "@/lib/api"

type ChatMessage = {
  role: "user" | "assistant"
  content: string
  citations?: Citation[]
}

// 右邊的問答區：輸入問題、顯示答案和引用的段落
// onUsageChanged：用量變了（問答花了錢），請上層重新查一次
export function QaPanel({
  workspaceId,
  onUsageChanged,
}: {
  workspaceId: number
  onUsageChanged: () => void
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [question, setQuestion] = useState("")
  const [asking, setAsking] = useState(false)
  // 目前這次問答的「取消開關」。useRef：存一個不需要觸發畫面更新的值，取消按鈕按下時拿來用
  const abortRef = useRef<AbortController | null>(null)

  async function ask() {
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
      onUsageChanged()
    } catch (err) {
      // 使用者自己按取消的，不用跳錯誤提示
      if (!controller.signal.aborted) {
        toast.error(err instanceof ApiError ? err.message : "問答失敗")
      }
      // 拿掉剛剛加上去的問題，並放回輸入框：使用者按一下就能重送，不用整句重打
      setMessages((prev) => prev.slice(0, -1))
      setQuestion(q)
    } finally {
      setAsking(false)
      abortRef.current = null
    }
  }

  // 按送出按鈕：preventDefault 讓表單不要整頁重新整理
  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    void ask()
  }

  // Enter 送出、Shift + Enter 換行
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      void ask()
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
              {messages.map((message, i) => (
                <ChatMessageView key={i} message={message} />
              ))}
              {asking && <Thinking onCancel={() => abortRef.current?.abort()} />}
            </div>
          )}
        </ScrollArea>
        <form onSubmit={handleSubmit} className="flex gap-2">
          <Textarea
            placeholder="輸入問題..."
            className="min-h-10 resize-none"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <Button type="submit" size="icon" disabled={asking}>
            <Send className="size-4" />
          </Button>
        </form>
      </CardContent>
    </Card>
  )
}

// 一則訊息：誰說的、內容；助理的答案再附上引用的段落
function ChatMessageView({ message }: { message: ChatMessage }) {
  const fromAssistant = message.role === "assistant"
  const citations = message.citations ?? []

  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-muted-foreground text-xs font-medium">
        {fromAssistant ? "助理" : "你"}
      </span>
      <p className="text-sm whitespace-pre-wrap">
        {fromAssistant ? withCitationMarks(message.content) : message.content}
      </p>
      {citations.length > 0 && (
        <div className="mt-1 flex flex-col gap-1.5">
          {/* 答案裡沒有 [n]：模型忘了標，後端退回列出搜到的全部段落，要讓使用者知道不一定都用到了 */}
          {!hasCitationMarks(message.content) && (
            <p className="text-muted-foreground text-xs">
              答案沒有逐句標註來源，以下是搜尋到的段落
            </p>
          )}
          {citations.map((citation) => (
            <CitationCard key={citation.number} citation={citation} />
          ))}
        </div>
      )}
    </div>
  )
}

// 一段引用：編號、檔名、內容的前兩行
function CitationCard({ citation }: { citation: Citation }) {
  return (
    <div className="bg-muted rounded-md p-2 text-xs">
      <span className="font-medium">
        <CitationMark number={citation.number} /> {citation.filename}
      </span>
      <p className="text-muted-foreground mt-0.5 line-clamp-2">{citation.content}</p>
    </div>
  )
}

// 等待回答時顯示，問答最久可能要等 1 分鐘，畫面完全沒動靜會讓人以為當掉了
// 取消：前端停止等待，把問題放回輸入框。後端已經送出的 OpenAI 請求收不回來，會在背景跑完
function Thinking({ onCancel }: { onCancel: () => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-muted-foreground text-xs font-medium">助理</span>
      <div className="flex items-center gap-2">
        <p className="text-muted-foreground animate-pulse text-sm">思考中…</p>
        <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={onCancel}>
          取消
        </Button>
      </div>
    </div>
  )
}

// 答案裡有沒有 [1] 這種引用標記
function hasCitationMarks(text: string) {
  return /\[\d+\]/.test(text)
}

// 逐句引用（docs/decisions/021）：答案裡的 [1] [2] 換成小標籤，跟下面引用卡片上的編號一樣
// split 用的正規表示式有括號（捕捉群組），切出來的陣列會保留 [1] 這些標記本身，奇數位置就是標記
function withCitationMarks(text: string) {
  return text.split(/(\[\d+\])/).map((part, i) =>
    i % 2 === 1 ? <CitationMark key={i} number={Number(part.slice(1, -1))} /> : part,
  )
}

function CitationMark({ number }: { number: number }) {
  return (
    <span className="bg-primary/10 text-primary mx-0.5 rounded px-1 text-[0.7rem] font-medium">
      {number}
    </span>
  )
}
