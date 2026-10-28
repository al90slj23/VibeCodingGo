/* Offline visual fixture only: no real conversation or host operation. */
window.VibeCueTranscript.update({
  theme: new URLSearchParams(window.location.search).get('theme') === 'light' ? 'light' : 'dark',
  state: 'ready',
  messages: [
    { id: 'fixture-user', role: 'user', phase: 'user', turn_id: 'fixture-turn', text: '把对话详情按 Codex 的格式整理一下，重点检查 Markdown、代码块和表格。', attachments: [{ name: '界面参考.png', path: '/private/tmp/fixture-reference.png', kind: 'image' }] },
    { id: 'fixture-progress', role: 'assistant', phase: 'commentary', turn_id: 'fixture-turn', text: '我先核对排版和内容来源，再检查输入区与阅读时的滚动行为。' },
    { id: 'fixture-answer', role: 'assistant', phase: 'final_answer', turn_id: 'fixture-turn', text: '已完成这轮整理。**对话内容回到阅读区中央**，附加操作放到需要时再打开。\n\n### 这次调整\n\n- 正常渲染标题、列表、引用和代码。\n- 阅读旧内容时保持当前位置；在底部时跟随最新回复。\n- 消息与代码都可以单独复制。\n\n```swift\nfunc continueConversation() {\n    print("从上次的后续需求继续")\n}\n```\n\n| 内容 | 显示方式 |\n| --- | --- |\n| 用户消息 | 靠右的中性文字块 |\n| 助手回复 | 连续正文，保留 Markdown 层次 |\n| 工具活动 | 收起后只显示摘要 |\n\n> 查看不等于处理完成。关闭窗口后，待续记录和草稿仍然保留。\n\n可以继续检查 [Conversation.swift](/private/tmp/Conversation.swift:12)，或查看 [Markdown 说明](https://commonmark.org/)。' }
  ],
  activities: [
    { id: 'fixture-tool-1', turn_id: 'fixture-turn', name: 'read', label: '已读取对话界面文件', status: 'complete' },
    { id: 'fixture-tool-2', turn_id: 'fixture-turn', name: 'exec', label: '已运行排版检查', status: 'complete' }
  ]
});
