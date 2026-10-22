---
name: pptx
description: 做演示文稿 / PPT / 汇报材料（.pptx）：多种版式、配色主题、图标、原生图表、配图；支持多轮修改
---

# 工作流程

1. **先想清楚再动手**：受众是谁、要他们记住什么。按 6～12 页规划故事线：封面 → 目录 → 分章节展开 → 结论 / 下一步 → 结尾。
   每页只讲一件事，**标题写结论**（"空置率连续三个季度下降"，而不是"空置率情况"）。
2. **配图（可选）**：封面、章节页、`image` 页和带图的要点页用照片会好看很多。可用时调用 `search_images`
   （关键词用具体名词，中英文都行，如 "city skyline night"、"团队 会议"），图片会放到 `/workspace/assets/images/`，
   工具结果里有路径。一份 PPT 搜 2～4 次就够（封面 1 次，章节 / 配图页各 1 次），一次要 2～3 张再挑。
   不要编造不存在的图片路径；搜不到合适的图就用图标和色块，不要反复重搜。
3. **写规格文件** `/workspace/work/deck.json`（格式见下）。**保留这个文件**：用户下一轮说"第三页改成图表""换个配色"时，
   读它、只改相关部分、重新生成，不要从头重写。
4. **生成**：`python /opt/skills/pptx/scripts/build.py /workspace/work/deck.json /workspace/outputs/<文件名>.pptx`
   - 输出里的 `WARN` 要处理（文字太长就精简或拆页，图标名不对就换），然后重新生成；改两轮后还剩个别 WARN 可以直接交付。
   - 不需要读生成器的源码，版式和字段以本说明为准。
   - 平台会在文件生成后**自动渲染预览**给用户看，你不需要自己转图片。
   - 修改时沿用同一个输出文件名，资料库会记录版本。
5. 用 `deliver_file` 交付，再用一两句话告诉用户做了什么、可以怎么继续改。

# 设计规则

- **主题按内容选**：`azure`（商务蓝，通用）、`emerald`（绿色，环保 / 城市 / 健康）、`sunset`（橙，活力 / 消费 / 营销）、
  `violet`（紫，科技 / 创新 / 设计）、`crimson`（红金，政务 / 党建 / 年度总结）、`graphite`（深灰蓝，技术 / 工程）、
  `midnight`（深色全篇，发布会 / 高端）。也可以自定义：`"theme": {"base": "azure", "primary": "#8B1E3F", "accent": "#E0A526"}`。
- **版式要有变化**：不要连续两页以上都是 `bullets`。并列的 3～6 点用 `cards`，数字用 `stats` / `big_number` / `chart`，
  对比用 `two_column`，步骤或计划用 `timeline`，真正的表格数据才用 `table`。
- **每页都要有视觉元素**：图标、图表、大数字、照片或色块，不要只有文字。
- **文字要少**：要点每条不超过 40 个字，每页不超过 5 条；细节放到 `notes`（演讲者备注）里。
- **数字要具体**：有数据就写出来（"+46%" 比"明显增长"有力），没有数据不要编造。

# 规格格式

顶层：`theme`、`title`（文档标题）、`footer`（页脚，默认等于 title）、`slides`（数组）。每页都可以加 `notes`（演讲者备注）。
内容页都可以加 `kicker`（标题上方的小标签，如"背景"）和 `subtitle`。

| layout | 字段 | 用途 |
|---|---|---|
| `cover` | `title` `subtitle` `kicker` `author` `date` `image` | 封面；有 image 时右半边是照片 |
| `agenda` | `title`（默认"目录"）`items`（≤7 条） | 目录 |
| `section` | `number`（如 "01"）`title` `subtitle` `image` | 章节过渡页 |
| `bullets` | `title` `bullets` + 可选右侧：`image` 或 `icon` + `highlight: {value, text}` | 要点；`"标题：正文"` 会把冒号前加粗；也可写 `{"title","text","sub":[...]}` |
| `cards` | `title` `cards: [{icon, title, text}]`（2～6 个；也可用 `value` 代替 icon） | 并列要点 / 能力 / 特点 |
| `two_column` | `title` `left/right: {heading, icon, bullets}` `vs: true` | 对比、前后、优劣 |
| `stats` | `title` `stats: [{value, label, note, icon}]`（2～4 个）`text` | 关键指标 |
| `big_number` | `title` `value` `label` `bullets` / `text` | 一个数字撑起一页 |
| `chart` | `title` `chart: {type, categories, series: [{name, values}], number_format, y_title}` `bullets`（右侧要点） | 原生可编辑图表；type: `column` `bar` `stacked` `line` `area` `pie` `doughnut` |
| `timeline` | `title` `steps: [{label, title, text, icon}]`（3～6 步） | 计划、里程碑、流程 |
| `table` | `title` `columns` `rows`（≤12 行） | 表格 |
| `quote` | `quote` `author` | 引言、用户原话 |
| `image` | `title` `caption` `image` | 整页大图 |
| `closing` | `title` `subtitle` `image` | 结尾页 |

图标名：运行 `python /opt/skills/pptx/scripts/build.py --icons` 查看（Lucide 图标，如 `rocket` `shield-check` `users`
`chart-line` `lightbulb` `target` `building-2` `leaf` `brain` `database` `globe` `handshake` `trophy` `wrench`）。
完整示例：`python /opt/skills/pptx/scripts/build.py --example`。

```json
{"theme": "emerald", "title": "老城区更新三年行动", "slides": [
  {"layout": "cover", "kicker": "季度汇报", "title": "老城区更新三年行动", "subtitle": "从街巷整治到产业焕新", "date": "2026 年 10 月",
   "image": "/workspace/assets/images/old_town_1.jpg"},
  {"layout": "cards", "title": "四条主线同步推进", "cards": [
    {"icon": "route", "title": "街巷微改造", "text": "拓宽消防通道，统一立面"},
    {"icon": "leaf", "title": "口袋公园", "text": "新增 8 处公共绿地"},
    {"icon": "store", "title": "业态引入", "text": "文创、餐饮与社区服务"}]},
  {"layout": "chart", "title": "空置率连续三个季度下降", "chart": {"type": "line", "categories": ["Q4", "Q1", "Q2", "Q3"],
   "series": [{"name": "示范街", "values": [27, 24, 19, 15]}]}, "bullets": ["降幅 12 个百分点"]}
]}
```
