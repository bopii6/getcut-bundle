# GetCut 素材包技能（getcut-bundle）

给 AI 助手用的技能：把任意内容——热点选题、网页、论文、聊天记录、已经写好的口播——整理成 [GetCut 成片台](https://gc.hellojoy.top) 能直接上传渲染的「素材包」。技能只负责把 ZIP 做在用户电脑上；用户随后在自己的电脑浏览器打开 GetCut，登录、选择 ZIP、确认文案和设置并开始生成。

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

装好后甩给助手一个选题或一份材料（「来一篇 XX 热点」「把这份 PDF 做成视频」）。它会按剧本走：先出选题候选和你确认 → 定稿口播 → 收集可商用素材 → 写清单 → 自校验 → 打包。打包完成后，助手把本地 ZIP 路径交给你；你在**自己的电脑浏览器**打开 GetCut，登录后选择这个 ZIP，确认文案与设置，再开始生成。技能不读取浏览器凭据，不索要密码或令牌，也不会自动上传。

## 对环境的要求

- 助手能联网**下载文件到本地**、能执行 shell 命令——只能聊天的助手做不了这件事
- 本机有 Python 3.10 或更高版本
- 环境里有 `ffmpeg` / `ffprobe`、能打 zip
- **想要图片质量好，自己去 [pexels.com/api](https://www.pexels.com/api) 免费申一把 key**，运行时用环境变量 `PEXELS_API_KEY` 传给技能里的抓取脚本。没有 key 也能跑，但会退回搜索引擎抓图，抽象词容易搜成字面物，质量明显下降。key 是自己的，技能包里不含任何 key
