"use client";
import { useState } from "react";
import { MessageSquare, Send } from "lucide-react";

interface Props {
  stepLabel: string;
  contextLabel?: string | null;
}

// 每步骤对话入口（画布底部条）：上下文 = 当前步骤 + 当前片段/对象（无 agent 名称）。
// 本期为占位：不接真实 agent / 内核（对话能力接入属 D02 未来设计），输入与发送分离、发送禁用。
export default function ConversationBar({ stepLabel, contextLabel }: Props) {
  const [text, setText] = useState("");
  return (
    <div className="workbench-conversation">
      <div className="workbench-conversation-step">
        <MessageSquare size={14} />
        <span className="workbench-conversation-step-label">{stepLabel}</span>
        {contextLabel ? (
          <span className="workbench-conversation-context">{contextLabel}</span>
        ) : null}
      </div>
      <div className="workbench-conversation-input">
        <input
          type="text"
          value={text}
          placeholder="就这一步提问或下达指令…"
          aria-label="就当前步骤提问"
          onChange={(e) => setText(e.target.value)}
        />
        <button
          type="button"
          className="button secondary"
          disabled
          title="对话能力接入待定（D02）"
        >
          <Send size={14} />
          发送
        </button>
      </div>
      <small className="workbench-conversation-note">
        对话能力接入待定（D02）· 本期不接真实 agent
      </small>
    </div>
  );
}
