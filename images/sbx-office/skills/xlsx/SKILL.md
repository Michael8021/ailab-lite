---
name: xlsx
title: 表格（Excel）
description: 做 Excel 表格 / 报表 / 数据分析（.xlsx）：表头样式、数字格式、公式、合计行、原生图表；支持多轮修改
---

# 工作流程

1. **弄清楚表给谁看、要回答什么问题**。一般一个汇总页在前，明细 / 原始数据在后。
2. **两种做法**：
   - **报表类**（数据不多、结构清楚）：写规格文件 `/workspace/work/book.json`，运行
     `python /opt/skills/xlsx/scripts/build_xlsx.py /workspace/work/book.json /workspace/outputs/<文件名>.xlsx`。
     示例：`python /opt/skills/xlsx/scripts/build_xlsx.py --example`。
   - **分析类**（要读输入文件、用 pandas 算）：写脚本 `/workspace/work/make_xlsx.py`，在脚本里用辅助函数：
     ```python
     import sys; sys.path.insert(0, "/opt/skills/xlsx/scripts")
     from build_xlsx import title_block, write_table, add_chart, autofit
     ```
     `write_table(ws, top, left, columns, rows, theme, total={"label": "合计", "sum": ["金额"]}, databar="金额")` 返回表格范围，
     `add_chart(ws, "column", table, [值列序号], "H3", "标题", data_rows=len(rows))` 加原生图表。
3. **保留规格文件或脚本**：用户下一轮说"加一列占比""按月份汇总"时，改它再重新生成，沿用同一个输出文件名。
4. 平台会在生成后**自动重算公式并渲染预览**给用户看；预览里出现 `#DIV/0!`、`#REF!` 之类错误时要修正。
5. 用 `deliver_file` 交付，并用一两句话说明表里有什么。

# 规则

- **用公式，不要把算好的结果写死**：合计、占比、增长率都写成公式，用户改输入时结果会跟着变。
  规格里公式可以用占位符：`{r}` 表示本行，`{first}` / `{last}` 表示数据区第一 / 最后一行，
  例如 `"=SUM(B{r}:D{r})"`、`"=E{r}/SUM(E{first}:E{last})"`。
- **数字格式**：`format` 可写 `int`（#,##0）、`number`（两位小数）、`yuan` / `money`（¥）、`usd`、`percent`（0.0%）、`pct`（0%）、
  `date`、`month`，或直接写 Excel 格式串。百分比存小数（0.125 显示为 12.5%）。年份写成文本或整数，不要加千分位。
- **样式统一**：同一个工作簿用同一个主题（`azure` `emerald` `graphite` `crimson` `violet`）；表头着色、隔行浅底、冻结表头已自动处理。
- 工作表名不超过 31 个字符；不要合并数据区的单元格（会妨碍排序筛选）。
- 原始数据不要改动，计算放在新列或新表里；数据来源和口径写在表下方的 `notes` 里。

# 规格格式

```json
{"theme": "azure", "sheets": [{
  "name": "销售汇总", "title": "2026 年三季度销售汇总", "subtitle": "单位：元",
  "columns": ["区域", {"name": "7月", "format": "yuan"}, {"name": "8月", "format": "yuan"}, {"name": "环比", "format": "percent"}],
  "rows": [["华东", 1200000, 1350000, "=C{r}/B{r}-1"], ["华南", 980000, 1010000, "=C{r}/B{r}-1"]],
  "total": {"label": "合计", "sum": ["7月", "8月"]},
  "databar": "8月",
  "charts": [{"type": "column", "title": "各区域 8 月销售额", "category": "区域", "values": ["8月"]}],
  "notes": ["数据来源：财务系统导出，截至 9 月 30 日"]
}]}
```

`charts[].type`：`column`（柱状）、`bar`（条形）、`line`（折线）、`pie`（饼图）；`values` 写列名，可以多列。
