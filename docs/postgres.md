# PostgreSQL + pgvector

数据库保存文档正文、来源元数据、分块和向量，LangChain `OpenAIEmbeddings` 负责 302.AI 向量化，`KnowledgeRetriever` 将数据库查询结果转换为 `Document` 交给 LangGraph。为保证文档与向量同一事务提交，数据库层使用 psycopg 和 pgvector SQL；未使用 `langchain-postgres` 自动创建的表。

## 本机运行

本机 Docker Desktop 启动遇到既有配置写入错误，因此数据库部署在 WSL 的 Ubuntu-22.04 中：PostgreSQL 14，独立集群 `power-agent`，端口 55432，pgvector 0.8.7。没有重置 Docker。应用仍在 Windows 的项目 `.venv` 中运行，通过本机端口连接数据库。

启动数据库和应用：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

`.env` 中 `POSTGRES_RUNTIME=wsl` 让启动脚本先检查并启动集群。脚本会在后台保留一个 WSL 进程，使 Windows 应用使用数据库期间 WSL 持续运行。手动在终端启动数据库可使用：

```powershell
wsl -d Ubuntu-22.04 -u root -- pg_ctlcluster --skip-systemctl-redirect --foreground 14 power-agent start
```

手动启动时保留该终端；通常直接使用 `start.ps1` 即可。数据库文件位于 WSL `/var/lib/postgresql/14/power-agent`。它独立于项目目录；备份项目文件不能替代 PostgreSQL 备份。

## 其他机器：Docker Compose

安装依赖，在 `.env` 设置 `POSTGRES_PASSWORD` 和带相同密码的 `DATABASE_URL`，设置 `POSTGRES_RUNTIME=docker`，然后运行 `start.ps1`。也可以分别运行：

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe migrate_postgres.py
.\.venv\Scripts\python.exe app.py
```

Compose 使用 `pgvector/pgvector:pg17`，端口只绑定 `127.0.0.1:55432`，数据写入独立命名卷。不要使用 `docker compose down -v`，这会删除数据库卷。已有外部 PostgreSQL 可直接填写连接串，并由管理员预先执行 `CREATE EXTENSION vector`。外部数据库的 SSL 和访问控制应按部署环境配置。

## 模型与配置

```dotenv
KNOWLEDGE_BACKEND=postgres
DATABASE_URL=postgresql://power_agent:YOUR_PASSWORD@127.0.0.1:55432/power_agent
DATABASE_SCHEMA=power_agent
EMBEDDING_BASE_URL=https://api.302.ai/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
EMBEDDING_API_KEY=
VECTOR_MIN_SIMILARITY=0.3
```

`EMBEDDING_API_KEY` 留空时沿用 `LLM_API_KEY`。导入时文档标题和分块正文会发给向量服务，检索时问题也会发送，产生相应用量；聊天模型继续使用原有配置。向量请求按最多 32 个片段批量发送，不自动重试。相同问题的成功向量在当前进程内缓存最多 128 条，重启后清空。LangSmith 上传关闭。

模型、接口、维度或分块策略不一致时拒绝使用旧索引。若更换向量模型，需选择新 `DATABASE_SCHEMA`，用保留的资料重新导入并验证，再切换；不要混用不同模型的向量。

## 数据一致性与迁移

- `documents`：原文、标题、文件名、来源链接、类型、摘要哈希、时间、文档 ID。
- `chunks`：分块正文、原文行号、中文双字词/英文词的全文索引、`vector(1536)` 向量；含 GIN 与 HNSW 索引。
- `settings`：向量配置指纹和首次初始化标记。

导入先完成文本提取、分块和向量化，再在一个事务中保存正文和所有片段；失败时不留下半成品。提取文本相同则去重并跳过向量调用；并发导入在提交阶段再次去重。删除文档通过外键级联删除片段与向量。支持 TXT/Markdown（1 MB）、PDF/DOCX（10 MB），单份提取文本最多 1 MB、最多 100 份资料。

PDF 使用 pypdf 按页提取；DOCX 使用 python-docx 按正文段落和表格行提取，之后进入相同的 LangChain 检索 / LangGraph 问答流程。位置标记保存在提取文本中，片段的 `location` 字段保存 PDF 物理页码或 DOCX 段落/表格行号；启动时自动为已有 chunks 表补充该字段，原资料不重新调用向量接口。文档解析在独立子进程执行，30 秒超时；PDF 最多 200 页，DOCX 解压总大小最多 40 MB、2000 个 ZIP 条目，提取区段最多 2000 个。临时二进制文件解析后清理，库中仅保存提取文本。扫描件需要先 OCR；不支持旧版 `.doc` 和加密文档。

运行 `migrate_postgres.py` 会先通过 SQLite backup API 创建一致性备份，保留源文件，逐份迁移并核对 ID、全文、哈希和来源等字段。成功记录在 `data/postgres-migration.json`；中断后可重复运行。应用不会在 PostgreSQL 连接失败时偷偷切回旧 SQLite。

数据库备份示例（输出仅包含本应用 schema）：

```powershell
wsl -d Ubuntu-22.04 -u root -- sh -c 'runuser -u postgres -- pg_dump -p 55432 -d power_agent -n power_agent -Fc > /tmp/power-agent-backup.dump'
```

随后将该文件复制到持久备份位置；还需单独妥善保存本机配置。旧 SQLite 只能还原迁移时的数据，不能恢复切换后新增的资料。

## 检索行为

在 PostgreSQL 内执行中文分词全文检索与向量余弦距离检索，按 RRF 合并排名；不是 BM25。相似度最低值为可配置的 0.3，仅是初始筛选阈值，不能当作回答置信度。检索质量仍需用实际电力问题评测。向量接口失败时保留关键词结果并显示降级提示；数据库不可用时返回服务错误。引用继续指向原文行号，PDF/DOCX 解析尚未包含在此次数据库迁移中。

## 验证

普通回归测试不访问付费模型：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真实数据库测试需在当前进程设置 `POSTGRES_TEST_URL`，然后运行相同命令。测试只创建并清理随机 `test_power_...` schema，使用确定性的假向量，不调用付费 API；覆盖持久化、语义检索、引用、事务回滚、级联删除、并发去重、配置冲突、故障降级和重复迁移。

参考：[pgvector](https://github.com/pgvector/pgvector)、[LangChain Embeddings](https://docs.langchain.com/oss/python/integrations/embeddings)、[302.AI Embeddings](https://302ai-en.apifox.cn/207705279e0)。
