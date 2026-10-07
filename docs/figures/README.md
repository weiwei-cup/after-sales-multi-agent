# 项目图示

README 使用两个互补视图：

- [系统架构](architecture.svg)：请求接纳、图运行、只读工具、受批准约束的事务执行与两个持久数据库。
- [Agent 工作流](agent-workflow.svg)：独立调查分支、事实依赖的政策核算、方案验证/审核与人工输入节点。

图示采用论文常见的白底、细线、统一 Arial / Helvetica 字体、矩形模块和面板编号。只用淡蓝区分 Agent、淡赭区分人工输入节点；不使用阴影、渐变、装饰图标或生成式图片。正文与图注使用中文，图内标签使用英文。

SVG 是可编辑的矢量源，PNG 为同一图的 2 倍分辨率渲染，可用于演示与文档。工作流只展示主要业务动作路径，省略异常转人工、无动作终止和部分恢复分支。服务架构是模块依赖视图，省略内部部分读写调用；完整实现与边界见 [系统架构](../technical-design.md)。

## 重绘

生成 SVG 只需要 Python 标准库：

```bash
uv run --locked python scripts/draw_readme_figures.py
uv run --locked python scripts/draw_readme_figures.py --check
```

修改模块或布局时编辑 [绘图源](../../scripts/draw_readme_figures.py)。导出 PNG 使用锁定开发依赖中的 Playwright，安装 Chromium 后执行：

```bash
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.tools/playwright-browsers"
uv run --locked playwright install chromium
uv run --locked python scripts/draw_readme_figures.py --render
```

渲染器检查文字是否超出模块与画布。提交前同时核对箭头、图例、标题与连接线；程序边界检查不能替代视觉审查。`--check` 核对 SVG 与绘图源一致，不修改文件或启动浏览器。
