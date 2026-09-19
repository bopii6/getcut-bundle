# GetCut 素材包技能（getcut-bundle）

给 AI 助手用的技能：把任意内容——热点选题、网页、论文、聊天记录、已经写好的口播——整理成 [GetCut 工作台](https://gc.hellojoy.top) 能直接渲染成片的「素材包」，打包完自动投进工作台，你只需要点「确定」。

## 安装：一句话

在任意**能执行命令**的 AI 助手里（ZCode、Claude Code、Cursor 等），把这句话发给它：

```text
帮我安装这个技能：https://github.com/bopii6/getcut-bundle
```

助手会自己把仓库放进它的技能目录（例如 `~/.agents/skills/getcut-bundle`，Claude Code 是 `~/.claude/skills/getcut-bundle`，以宿主约定为准）并读取 `SKILL.md`。也可以手动装：

```bash
git clone https://github.com/bopii6/getcut-bundle ~/.agents/skills/getcut-bundle
```

## 用法

装好后甩给助手一个选题或一份材料（「来一篇 XX 热点」「把这份 PDF 做成视频」）。它会按剧本走：先出选题候选和你确认 → 定稿口播 → 收集可商用素材 → 写清单 → 自校验 → 打包 → 自动投喂到 GetCut 工作台，你点「确定，导入工作台」→「确认内容并开始生成」，剩下的出片交给工作台。

## 对环境的要求

- 助手能联网**下载文件到本地**、能执行 shell 命令——只能聊天的助手（比如手机 App 里的）做不了这件事
- 环境里有 `ffmpeg` / `ffprobe`、能打 zip
