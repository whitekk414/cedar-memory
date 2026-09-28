# Cedar Memory 首版实施清单

**Goal:** 独立 Python 包，显式偏好管理、作用域隔离、摘要持久化与按需召回。
**Architecture:** SQLite 每操作独立连接和短事务；作用域修订号使旧摘要失效。异步门面通过线程执行本地 I/O，摘要回调由业务注入，不绑定网络供应商。
**Tech Stack:** Python 3.11+ 标准库 sqlite3、asyncio、dataclasses、unittest，setuptools 打包。

## 约定

- 在独立空仓库的 feat/initial-memory 分支实现，不复制 Nebula 源码。
- scope 由可信业务侧提供；SDK 不是身份认证或密钥检测工具。
- 写入仅支持显式调用，不自动提取自然语言。当前请求指令不由 SDK 改写。
- 偏好变更递增 scope revision 并清除该 scope 的摘要；异步摘要保存必须校验开始时 revision，拒绝旧任务复活信息。
- SQLite 文件仅本机使用；不持久化 pending，不同步其他 Station。

## 实施顺序

- [x] 1. 在 tests/test_store.py 写持久化、隔离、覆盖、删除、校验、摘要版本和并发测试；先执行观察失败，再在 models.py、store.py 实现。
- [x] 2. 在 tests/test_memory.py 写异步门面、显式召回、长度限制、摘要阈值、超时和并发失效测试；先失败，再在 memory.py 实现。
- [x] 3. 新增 README.md、docs/integration.md、examples/basic.py 和 CI；说明摘要输入必须排除待忘记的旧事实，调用方历史也必须清理。
- [x] 4. 执行 `python -m unittest discover -s tests -v`，构建 wheel 并在隔离环境安装运行示例；核对无公司源码和本地数据进入 Git。
- [x] 5. 进行独立代码审查并修复问题；保留开发分支，未经单独确认不推送或合并。

## 测试契约

存储测试示例：`store.put('person:a', 'light', 'warm', source_request_id='req-1')` 后重开数据库，`store.get('person:a', 'light').value == 'warm'`；另一个 scope 查不到。

摘要测试示例：先读 `revision = store.revision(scope)`；修改偏好后 `store.save_summary(..., expected_revision=revision)` 必须抛出 `StaleSummaryError`，不能写入过期摘要。

异步测试示例：`await memory.summarize(scope, session_id, turns, covered_through=8, summarizer=callback)` 达到阈值才调用 callback；用事件控制 callback 期间删记忆，完成时必须拒绝保存旧摘要。

验收命令使用真实临时 SQLite 文件，不依赖模型、网络、Nebula 或密钥。摘要文本长度按 Unicode 字符限制，不宣称 token 预算或模型安全保证。
