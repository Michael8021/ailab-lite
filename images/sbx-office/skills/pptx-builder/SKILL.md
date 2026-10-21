---
name: pptx-builder
description: 生成可编辑的 PowerPoint（.pptx），中文排版，含渲染预览自检
---

# 何时使用
用户要求做 PPT、汇报、演示文稿时。

# 步骤
1. 先写大纲（每页：标题 + 3～5 条要点，每条不超过 30 字）。
2. 把大纲写成 JSON，结构：`{"title": "...", "slides": [{"title": "...", "bullets": ["..."]}]}`，保存到 `/workspace/outline.json`。
3. 运行：`python /opt/skills/pptx-builder/scripts/build.py /workspace/outline.json /workspace/outputs/<文件名>.pptx`
4. 预览自检：`python /opt/skills/pptx-builder/scripts/preview.py /workspace/outputs/<文件名>.pptx`，
   它会输出 PNG 路径和溢出检查结果；有 `OVERFLOW` 就精简对应页的文字后重新生成。
5. 完成后调用 `deliver_file` 交付 `outputs/` 下的文件。

# 版式约束
- 字体写“微软雅黑”（沙箱里映射到 Noto Sans CJK）。
- 标题 32pt，正文 18～20pt；每页要点不超过 6 条。
